import os
import jwt
import datetime as dt
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import User, get_session

# 加载环境变量
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# 密码哈希上下文（bcrypt）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    """生成密码哈希（bcrypt）"""

    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配"""

    try:
        return pwd_context.verify(plain_password, password_hash)
    except Exception:
        return False


def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    """生成 JWT Token"""

    to_encode = data.copy()
    expire = dt.datetime.utcnow() + dt.timedelta(
        minutes=expires_minutes or ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme), session: AsyncSession = Depends(get_session)
):
    """
    获取当前用户（用于需要登录的 HTTP 接口）
    - 从 Authorization: Bearer 中解析 JWT
    - 查询用户信息并返回 ORM 实例
    """

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌")

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


def decode_token_user_id(token: str) -> Optional[int]:
    """工具函数：从 Token 中解析用户 ID，失败返回 None"""

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except Exception:
        return None


# Redis 客户端单例（异步）
_redis_client: Optional[Redis] = None


async def get_redis() -> Redis:
    """获取 Redis 异步客户端（单例）"""

    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 客户端连接"""
    global _redis_client
    try:
        if _redis_client is not None:
            await _redis_client.close()
            _redis_client = None
    except Exception:
        pass
