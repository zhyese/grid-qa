"""异步数据库会话（SQLAlchemy 2.0 + aiomysql）。"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,   # 自动探测断连，避免 MySQL 8 旧连接报错
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db():
    """FastAPI 依赖：提供一个事务会话并在结束后关闭。"""
    async with AsyncSessionLocal() as session:
        yield session
