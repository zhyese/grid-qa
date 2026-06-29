"""建表 + 初始化默认管理员。启动时由 lifespan 调用。"""
from sqlalchemy import select

from app.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
from app.models.document import Document  # noqa: F401  确保表被注册
from app.models.operation_log import OperationLog  # noqa: F401  确保表被注册
from app.models.user import User  # noqa: F401


async def init_db() -> None:
    # 1. 建表（开发期用 create_all；生产可换 Alembic）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 初始管理员（不存在则创建）
    async with AsyncSessionLocal() as db:
        exists = (
            await db.execute(select(User).where(User.username == settings.ADMIN_USERNAME))
        ).scalar_one_or_none()
        if not exists:
            db.add(
                User(
                    username=settings.ADMIN_USERNAME,
                    password_hash=hash_password(settings.ADMIN_PASSWORD),
                    role="admin",
                )
            )
            await db.commit()
            print(f"[init_db] 已创建默认管理员：{settings.ADMIN_USERNAME} / {settings.ADMIN_PASSWORD}")
