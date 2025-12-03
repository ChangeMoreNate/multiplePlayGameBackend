from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class UserCreate(BaseModel):
    """用户注册请求模型"""

    username: str = Field(min_length=3, max_length=64, description="用户名")
    password: str = Field(min_length=6, max_length=128, description="密码")


class UserLogin(BaseModel):
    """用户登录请求模型"""

    username: str
    password: str


class Token(BaseModel):
    """登录成功返回的 JWT"""

    access_token: str
    token_type: str = "bearer"


class PlayerState(BaseModel):
    """单个玩家的状态模型"""

    player_id: str
    x: float
    y: float
    color: str


class BroadcastState(BaseModel):
    """房间状态广播消息模型"""

    type: str = "state"
    players: List[PlayerState]


class JoinLeaveMessage(BaseModel):
    """加入/离开消息模型"""

    type: str
    player_id: str


class ErrorMessage(BaseModel):
    """错误消息模型"""

    type: str = "error"
    message: str


class PongMessage(BaseModel):
    """心跳回应消息模型"""

    type: str = "pong"


class UserOut(BaseModel):
    """对外展示的用户模型（示例）"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: Optional[str] = None
