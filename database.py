import os
from typing import AsyncGenerator

# 导入 .env 环境变量支持
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime, UniqueConstraint, func, text
from sqlalchemy.engine.url import make_url, URL
import datetime as dt

# 读取数据库连接字符串，提供默认值以便快速运行
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+asyncmy://root:password@127.0.0.1:3306/game",
)

# 创建异步引擎。pool_pre_ping 保证连接可用性。
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


class Base(DeclarativeBase):
    """SQLAlchemy Declarative Base"""


class User(Base):
    """
    用户表模型（MySQL）
    - 包含唯一用户名与密码哈希
    - created_at 自动填充
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# 会话工厂，expire_on_commit=False 可避免对象在提交后失效
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI 依赖：获取异步数据库会话
    - 自动处理会话生命周期
    - 异常抛出由上层捕获
    """

    async with async_session() as session:
        try:
            yield session
        except Exception:
            raise
