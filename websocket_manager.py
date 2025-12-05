import asyncio
import time
import random
import json
import math
from uuid import uuid4
from typing import Dict, Any, Optional

from fastapi import WebSocket
from redis.asyncio import Redis


class RoomState:
    """
    房间内状态（内存结构）
    - connections: player_id -> WebSocket
    - players: player_id -> {x, y, color, last_seen}
    - lock: 房间级别的异步锁，保证并发安全
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.connections: Dict[str, WebSocket] = {}
        self.players: Dict[str, Dict[str, Any]] = {}
        self.ball: Optional[Dict[str, Any]] = None
        self.balls: Dict[str, Dict[str, Any]] = {}
        self.world_width: Optional[float] = None
        self.world_height: Optional[float] = None
        self.game_started: bool = False
        self.state_dirty: bool = False
        self.state_task: Optional[asyncio.Task] = None
        self.broadcast_interval: float = 0.05


class WebSocketRoomManager:
    """
    房间管理器
    - 负责加入/离开、广播、位置更新、心跳踢人
    - Redis 用于持久化与跨进程共享玩家状态
    """

    def __init__(self, redis: Redis, kick_timeout_seconds: int = 60, scan_interval_seconds: int = 10) -> None:
        self.redis = redis
        self.rooms: Dict[str, RoomState] = {}
        self.kick_timeout_seconds = kick_timeout_seconds
        self.scan_interval_seconds = scan_interval_seconds
        self.ball_damping = 0.03
        self.room_capacity = 2
        self.room_names: Dict[str, str] = {}

    def _ensure_room(self, room_id: str) -> RoomState:
        if room_id not in self.rooms:
            self.rooms[room_id] = RoomState()
        return self.rooms[room_id]

    async def join(self, room_id: str, player_id: str, websocket: WebSocket) -> str:
        """
        加入房间：分配颜色、初始化坐标、写入 Redis、广播 join 与当前 state
        返回分配的颜色字符串
        """

        room = self._ensure_room(room_id)
        async with room.lock:
            color = f"#{random.randint(0, 0xFFFFFF):06x}"
            player_type = "A" if not room.players else "B"
            if len(room.connections) >= self.room_capacity:
                raise RuntimeError("room_full")
            room.connections[player_id] = websocket
            if room.world_width and room.world_height:
                cx = float(room.world_width) / 2.0
                ry = 5.0 / 6.0 if str(player_type) == "A" else (1.0 / 6.0)
                cy = float(room.world_height) * ry
                ix, iy = cx, cy
            else:
                ix, iy = 0.0, 0.0
            room.players[player_id] = {
                "x": ix,
                "y": iy,
                "color": color,
                "player_type": player_type,
                "last_seen": time.time(),
            }

            # 写入 Redis
            try:
                await self.redis.sadd(f"room:{room_id}:members", player_id)
                await self.redis.hset(
                    f"room:{room_id}:player:{player_id}",
                    mapping={"x": ix, "y": iy, "color": color, "player_type": player_type, "last_seen": time.time()},
                )
            except Exception:
                pass

        # 锁外广播，避免死锁
        await self._broadcast(room_id, {"type": "join", "player_id": player_id, "player_type": player_type})
        try:
            async with room.lock:
                room.state_dirty = True
        except Exception:
            pass
        await self._ensure_state_task(room_id)

        return color

    async def get_player_count(self, room_id: str) -> int:
        now = time.time()
        try:
            members = await self.redis.smembers(f"room:{room_id}:members")
            if members:
                active = 0
                for pid in members:
                    try:
                        ls = await self.redis.hget(f"room:{room_id}:player:{pid}", "last_seen")
                        if ls is not None and now - float(ls) <= self.kick_timeout_seconds:
                            active += 1
                    except Exception:
                        pass
                return active
        except Exception:
            pass
        room = self._ensure_room(room_id)
        return len(room.connections)

    async def is_room_full(self, room_id: str) -> bool:
        cnt = await self.get_player_count(room_id)
        return cnt >= self.room_capacity

    async def leave(self, room_id: str, player_id: str) -> None:
        """离开房间：清理内存与 Redis，并广播 leave"""

        room = self._ensure_room(room_id)
        # 仅在锁内修改内存结构，避免在持锁时执行耗时 await
        async with room.lock:
            ws: Optional[WebSocket] = room.connections.pop(player_id, None)
            room.players.pop(player_id, None)
            room.balls.pop(player_id, None)

        # 在锁外关闭连接，并设置短超时避免阻塞退出
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=0.3)
            except Exception:
                pass

        # 在锁外执行 Redis 清理，避免持锁等待 IO
        try:
            await self.redis.srem(f"room:{room_id}:members", player_id)
            await self.redis.delete(f"room:{room_id}:player:{player_id}")
            await self.redis.delete(f"room:{room_id}:ball:{player_id}")
        except Exception:
            pass

        # 锁外广播，避免死锁
        await self._broadcast(room_id, {"type": "leave", "player_id": player_id})
        try:
            async with room.lock:
                room.state_dirty = True
        except Exception:
            pass
        await self._ensure_state_task(room_id)
        try:
            async with room.lock:
                if not room.connections and room.state_task is not None:
                    room.state_task.cancel()
        except Exception:
            pass

    async def touch(self, room_id: str, player_id: str) -> None:
        """刷新玩家心跳时间戳"""

        room = self._ensure_room(room_id)
        now = time.time()
        async with room.lock:
            if player_id in room.players:
                room.players[player_id]["last_seen"] = now
        try:
            await self.redis.hset(f"room:{room_id}:player:{player_id}", mapping={"last_seen": now})
        except Exception:
            pass

    async def update_position(self, room_id: str, player_id: str, x: float, y: float) -> None:
        """
        更新玩家坐标（内存与 Redis），并广播房间最新 state
        """

        room = self._ensure_room(room_id)
        async with room.lock:
            if player_id not in room.players:
                return
            room.players[player_id]["x"] = float(x)
            room.players[player_id]["y"] = float(y)
            room.players[player_id]["last_seen"] = time.time()

            try:
                await self.redis.hset(
                    f"room:{room_id}:player:{player_id}", mapping={"x": float(x), "y": float(y), "last_seen": time.time()}
                )
            except Exception:
                pass

        try:
            async with room.lock:
                room.state_dirty = True
        except Exception:
            pass
        await self._ensure_state_task(room_id)

    async def broadcast_state(self, room_id: str) -> None:
        """广播当前房间内所有玩家的最新状态"""

        room = self._ensure_room(room_id)
        # 构建状态快照（避免长时间持锁）
        async with room.lock:
            players_payload = [
                {
                    "player_id": pid,
                    "x": info.get("x", 0.0),
                    "y": info.get("y", 0.0),
                    "color": info.get("color", "#ffffff"),
                    "player_type": info.get("player_type", "A"),
                }
                for pid, info in room.players.items()
            ]

        await self._broadcast(room_id, {"type": "state", "players": players_payload})

    async def _ensure_state_task(self, room_id: str) -> None:
        room = self._ensure_room(room_id)
        async with room.lock:
            task = room.state_task
            if task is None or task.done():
                room.state_task = asyncio.create_task(self._state_loop(room_id))

    async def _state_loop(self, room_id: str) -> None:
        room = self._ensure_room(room_id)
        try:
            while True:
                await asyncio.sleep(room.broadcast_interval)
                dirty = False
                async with room.lock:
                    dirty = room.state_dirty
                    room.state_dirty = False
                if dirty:
                    await self.broadcast_state(room_id)
        except asyncio.CancelledError:
            pass

    async def record_ball(self, room_id: str, x: float, y: float, vx: float, vy: float) -> None:
        """记录房间当前球状态并广播给房间内所有人"""

        ts = time.time()
        room = self._ensure_room(room_id)
        prev_ts = room.ball.get("ts") if room.ball else None
        dt = ts - prev_ts if isinstance(prev_ts, (int, float)) else 0.0
        factor = math.exp(-self.ball_damping * dt) if dt > 0 else 1.0
        vx = float(vx) * factor
        vy = float(vy) * factor
        async with room.lock:
            room.ball = {
                "x": float(x),
                "y": float(y),
                "vx": float(vx),
                "vy": float(vy),
                "ts": ts,
            }
            try:
                await self.redis.hset(
                    f"room:{room_id}:ball",
                    mapping={"x": float(x), "y": float(y), "vx": float(vx), "vy": float(vy), "ts": ts},
                )
            except Exception:
                pass

        await self._broadcast(
            room_id,
            {"type": "ball", "x": float(x), "y": float(y), "vx": float(vx), "vy": float(vy), "ts": ts},
        )

    async def _broadcast(self, room_id: str, message: Dict[str, Any]) -> None:
        """向房间内所有连接广播消息，忽略单个发送错误"""

        room = self._ensure_room(room_id)
        # 复制连接列表，避免发送过程中集合被修改
        async with room.lock:
            conns = list(room.connections.items())

        if not conns:
            return

        send_tasks = []
        for pid, ws in conns:
            send_tasks.append(self._safe_send(ws, message))
        await asyncio.gather(*send_tasks, return_exceptions=True)

    @staticmethod
    async def _safe_send(ws: WebSocket, message: Dict[str, Any]) -> None:
        try:
            await ws.send_json(message)
        except Exception:
            # 忽略单个连接的发送错误
            pass

    async def kick_inactive_loop(self) -> None:
        """
        后台任务：周期性检查超时未心跳的玩家并踢出
        - 超过 self.kick_timeout_seconds 无任何消息则关闭连接并移除
        """

        while True:
            try:
                now = time.time()
                # 遍历所有房间与玩家
                for room_id, room in list(self.rooms.items()):
                    async with room.lock:
                        inactive = [
                            pid
                            for pid, info in room.players.items()
                            if now - float(info.get("last_seen", 0)) > self.kick_timeout_seconds
                        ]
                    for pid in inactive:
                        await self.leave(room_id, pid)
                    try:
                        redis_members = await self.redis.smembers(f"room:{room_id}:members")
                        if redis_members:
                            stale = []
                            for pid in redis_members:
                                try:
                                    ls = await self.redis.hget(f"room:{room_id}:player:{pid}", "last_seen")
                                    if ls is None or now - float(ls) > self.kick_timeout_seconds:
                                        stale.append(pid)
                                except Exception:
                                    stale.append(pid)
                            for pid in stale:
                                try:
                                    await self.redis.srem(f"room:{room_id}:members", pid)
                                    await self.redis.delete(f"room:{room_id}:player:{pid}")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                await asyncio.sleep(self.scan_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(self.scan_interval_seconds)
    async def set_world(self, room_id: str, width: float, height: float) -> None:
        room = self._ensure_room(room_id)
        async with room.lock:
            room.world_width = float(width)
            room.world_height = float(height)

    async def start_game(self, room_id: str) -> bool:
        room = self._ensure_room(room_id)
        started_now = False
        async with room.lock:
            if not room.game_started:
                room.game_started = True
                started_now = True
                try:
                    await self.redis.hset(f"room:{room_id}:meta", mapping={"game_started": "1"})
                except Exception:
                    pass
        if started_now:
            await self._broadcast(room_id, {"type": "game_started"})
        return started_now

    async def shutdown(self) -> None:
        try:
            for room_id, room in list(self.rooms.items()):
                try:
                    async with room.lock:
                        task = room.state_task
                    if task is not None:
                        task.cancel()
                        try:
                            await task
                        except Exception:
                            pass
                except Exception:
                    pass
                async with room.lock:
                    conns = list(room.connections.items())
                    players = list(room.players.keys())
                for pid, ws in conns:
                    try:
                        await asyncio.wait_for(ws.close(), timeout=0.3)
                    except Exception:
                        pass
                try:
                    if players:
                        await self.redis.srem(f"room:{room_id}:members", *players)
                    for pid in players:
                        await self.redis.delete(f"room:{room_id}:player:{pid}")
                except Exception:
                    pass
                async with room.lock:
                    room.connections.clear()
                    room.players.clear()
                    room.ball = None
                    room.state_task = None
                    room.state_dirty = False
        except Exception:
            pass

    async def create_room(self, name: str) -> str:
        room_id = uuid4().hex
        room = self._ensure_room(room_id)
        async with room.lock:
            self.room_names[room_id] = name
        try:
            await self.redis.sadd("rooms", room_id)
            await self.redis.hset(f"room:{room_id}:meta", mapping={"name": name})
        except Exception:
            pass
        return room_id

    async def get_rooms(self) -> list:
        ids = set()
        try:
            redis_ids = await self.redis.smembers("rooms")
            ids.update(redis_ids or [])
        except Exception:
            pass
        ids.update(self.rooms.keys())
        result = []
        for rid in ids:
            name = self.room_names.get(rid)
            if not name:
                try:
                    name = await self.redis.hget(f"room:{rid}:meta", "name")
                except Exception:
                    name = None
            if not name:
                name = rid
            try:
                count = await self.get_player_count(rid)
            except Exception:
                count = len(self.rooms[rid].connections) if rid in self.rooms else 0
            result.append({"room_id": rid, "name": name, "player_count": min(count, 2)})
        return result
