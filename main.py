import os
import asyncio
import json
from typing import Optional
import time

# 加载 .env 环境变量
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import engine, Base, async_session, get_session, User
from deps import (
    get_redis,
    create_access_token,
    verify_password,
    get_password_hash,
    decode_token_user_id,
)
from websocket_manager import WebSocketRoomManager
from models import UserCreate, UserLogin, Token


app = FastAPI(title="Realtime Position Sync Demo")

# CORS 配置：允许来自环境变量指定的来源（默认 *）
cors_origins_env = os.getenv("CORS_ORIGINS", "*")
cors_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()] if cors_origins_env else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    """
    启动事件：
    - 创建数据库表
    - 初始化 Redis 客户端
    - 启动心跳踢人后台任务
    """

    # 创建数据库（若不存在）与表
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"[启动警告] 数据库表创建失败：{e}")

    # 初始化 Redis 客户端与房间管理器
    redis = await get_redis()
    try:
        await redis.ping()
    except Exception as e:
        print(f"[启动警告] Redis 连接失败：{e}")

    app.state.ws_manager = WebSocketRoomManager(redis)
    app.state.kick_task = asyncio.create_task(app.state.ws_manager.kick_inactive_loop())


@app.on_event("shutdown")
async def on_shutdown():
    """关闭事件：取消后台任务"""

    try:
        task = getattr(app.state, "kick_task", None)
        if task:
            task.cancel()
    except Exception:
        pass


@app.post("/api/register")
async def register(user: UserCreate, session: AsyncSession = Depends(get_session)):
    """
    用户注册
    - 校验用户名唯一
    - 存储密码哈希
    """

    try:
        # 查重用户名
        existing = await session.execute(select(User).where(User.username == user.username))
        if existing.scalar_one_or_none():
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "用户名已存在"})

        # 保存用户
        new_user = User(username=user.username, password_hash=get_password_hash(user.password))
        session.add(new_user)
        await session.commit()
        return {"message": "注册成功"}
    except Exception as e:
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": f"注册失败: {str(e)}"})


@app.post("/api/login", response_model=Token)
async def login(payload: UserLogin, session: AsyncSession = Depends(get_session)):
    """
    用户登录
    - 校验用户名与密码
    - 返回 JWT Token
    """

    try:
        result = await session.execute(select(User).where(User.username == payload.username))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

        token = create_access_token({"sub": str(user.id)})
        return Token(access_token=token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"登录失败: {e}")


def _extract_token(websocket: WebSocket) -> Optional[str]:
    """从 WebSocket Header 或 Query 中提取 Bearer Token"""
    # Header 优先
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # Query 其次
    token = websocket.query_params.get("token")
    if token:
        return token
    return None


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """
    WebSocket 连接：
    - 连接时必须携带 JWT（Header Bearer 或 ?token）
    - 加入房间并广播 join/state
    - 处理 move/ping 消息；广播 state 或回复 pong
    - 断开或超时自动 leave 并广播
    """
    
    # 先接受连接，使客户端能够收到服务端的提示帧
    try:
        await websocket.accept()
    except Exception:
        return

    token = _extract_token(websocket)
    if not token:
        try:
            await websocket.send_json({"type": "auth", "message": "false"})
            await asyncio.sleep(0.2)
        except Exception:
            pass
        await websocket.close(code=4401, reason="auth_failed")
        return

    # 解析用户信息
    user_id = decode_token_user_id(token)
    if user_id is None:
        try:
            await websocket.send_json({"type": "auth", "message": "false"})
            await asyncio.sleep(0.2)
        except Exception:
            pass
        await websocket.close(code=4401, reason="auth_failed")
        return

    # 连接已在前面 accept
    manager: WebSocketRoomManager = app.state.ws_manager
    player_id = str(user_id)

    # 加入房间并广播
    try:
        await manager.join(room_id, player_id, websocket)
    except Exception:
        # 无法加入则关闭
        await websocket.close()
        return
    # 消息循环
    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue

            msg_type = str(data.get("type", "")).lower()

            if msg_type == "move":
                try:
                    x = float(data.get("x", 0.0))
                    y = float(data.get("y", 0.0))
                except Exception:
                    continue
                await manager.update_position(room_id, player_id, x, y)
            elif msg_type == "ping":
                # 在心跳时验证 Token 是否仍有效，若失效则通知前端并断开
                if decode_token_user_id(token) is None:
                    try:
                        await websocket.send_json({"type": "auth", "message": "false"})
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
                    await websocket.close(code=1008)
                    break
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    pass
                await manager.touch(room_id, player_id)
            else:
                try:
                    await websocket.send_json({"type": "error", "message": "未知消息类型"})
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        # 保底异常防护
        pass
    finally:
        # 清理与离开广播
        try:
            await manager.leave(room_id, player_id)
        except Exception:
            pass
