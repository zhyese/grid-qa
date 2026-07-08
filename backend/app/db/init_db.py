"""建表 + 初始化默认管理员。启动时由 lifespan 调用。"""
from sqlalchemy import select, text

from app.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
from app.models.chunk import Chunk  # noqa: F401  确保表被注册
from app.models.conversation import Conversation, Message  # noqa: F401  确保表被注册
from app.models.document import Document  # noqa: F401  确保表被注册
from app.models.document_version import DocumentVersion  # noqa: F401
from app.models.feedback import Feedback  # noqa: F401  确保表被注册
from app.models.kg_triple import KgTriple  # noqa: F401  确保表被注册
from app.models.operation_log import OperationLog  # noqa: F401  确保表被注册
from app.models.qa_cache import QaCache  # noqa: F401  确保表被注册
from app.models.user import User  # noqa: F401
from app.models.agent_tool_call import AgentToolCall  # noqa: F401  S4 工具调用审计
from app.models.alert_disposal import AlertDisposal  # noqa: F401  S3 告警处置


# 现有表加列的幂等迁移（create_all 只建不 ALTER；老库靠这里补列，列已存在则忽略 1060 错误）。
# 格式：(表, 列, 类型定义)。新增结构感知分块 / 反馈闭环 / 设备台账字段。
_COLUMN_MIGRATIONS = [
    ("chunks", "chunk_type", "VARCHAR(16) NOT NULL DEFAULT 'child'"),
    ("chunks", "parent_idx", "INT NOT NULL DEFAULT 0"),
    ("chunks", "section", "VARCHAR(256) NOT NULL DEFAULT ''"),
    ("feedbacks", "reason", "VARCHAR(256) NOT NULL DEFAULT ''"),
    ("feedbacks", "judge_supported", "FLOAT"),
    ("feedbacks", "judge_halluc", "FLOAT"),
    ("documents", "equipment_tags", "VARCHAR(512) NOT NULL DEFAULT ''"),
    ("documents", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("users", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("conversations", "is_deleted", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("messages", "is_deleted", "TINYINT(1) NOT NULL DEFAULT 0"),
]


async def _ensure_columns() -> None:
    """幂等补列：逐条 ALTER，列已存在时 MySQL 报 1060，忽略即可。"""
    async with engine.begin() as conn:
        for table, col, typedef in _COLUMN_MIGRATIONS:
            try:
                await conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {typedef}"))
            except Exception:
                pass  # 列已存在（1060）或表不存在，跳过


async def init_db() -> None:
    # 1. 建表（开发期用 create_all；生产可换 Alembic）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 现有表补列（幂等，老库演进用）
    await _ensure_columns()

    # 3. 初始管理员（不存在则创建）
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
