import asyncio
import time
import random
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
            # 分配随机颜色，初始化坐标
            color = f"#{random.randint(0, 0xFFFFFF):06x}"
            room.connections[player_id] = websocket
            room.players[player_id] = {
                "x": 0.0,
                "y": 0.0,
                "color": color,
                "last_seen": time.time(),
            }

            # 写入 Redis
            try:
                await self.redis.sadd(f"room:{room_id}:members", player_id)
                await self.redis.hset(
                    f"room:{room_id}:player:{player_id}", mapping={"x": 0.0, "y": 0.0, "color": color}
                )
            except Exception:
                # Redis 异常不阻断加入流程
                pass

        # 锁外广播，避免死锁
        await self._broadcast(room_id, {"type": "join", "player_id": player_id})
        await self.broadcast_state(room_id)

        return color

    async def leave(self, room_id: str, player_id: str) -> None:
        """离开房间：清理内存与 Redis，并广播 leave"""

        room = self._ensure_room(room_id)
        async with room.lock:
            # 关闭连接（若仍存在）
            ws: Optional[WebSocket] = room.connections.pop(player_id, None)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

            room.players.pop(player_id, None)

            # 清理 Redis
            try:
                await self.redis.srem(f"room:{room_id}:members", player_id)
                await self.redis.delete(f"room:{room_id}:player:{player_id}")
            except Exception:
                pass

        # 锁外广播，避免死锁
        await self._broadcast(room_id, {"type": "leave", "player_id": player_id})
        await self.broadcast_state(room_id)

    async def touch(self, room_id: str, player_id: str) -> None:
        """刷新玩家心跳时间戳"""

        room = self._ensure_room(room_id)
        async with room.lock:
            if player_id in room.players:
                room.players[player_id]["last_seen"] = time.time()

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
                    f"room:{room_id}:player:{player_id}", mapping={"x": float(x), "y": float(y)}
                )
            except Exception:
                pass

        # 广播最新状态（锁外广播以减少阻塞）
        await self.broadcast_state(room_id)

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
                }
                for pid, info in room.players.items()
            ]

        await self._broadcast(room_id, {"type": "state", "players": players_payload})

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
                        # 调用 leave 进行清理与广播
                        await self.leave(room_id, pid)
                await asyncio.sleep(self.scan_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                # 避免任务因异常退出
                await asyncio.sleep(self.scan_interval_seconds)
