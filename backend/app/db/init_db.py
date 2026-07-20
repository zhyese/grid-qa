"""建表 + 初始化默认管理员。启动时由 lifespan 调用。"""
from sqlalchemy import select, text

from app.config import settings
from app.models.domain_event import DomainEvent, EventDelivery  # noqa: F401
from app.models.knowledge_governance import (  # noqa: F401
    KnowledgeDocumentMetadata,
    KnowledgeGovernanceIssue,
    KnowledgeGovernanceReview,
)
from app.models.persistent_task import PersistentTask  # noqa: F401
from app.models.knowledge_evolution import KnowledgeEvolutionDraft  # noqa: F401  知识自进化草稿
from app.models.realtime_event import (  # noqa: F401
    ProactiveOpsRun,
    RealtimeDeviceMapping,
    RealtimeEvent,
)
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
from app.models.ticket import Ticket  # noqa: F401  确保两票表与来源幂等约束被注册
from app.models.user import User  # noqa: F401
from app.models.agent_tool_call import AgentToolCall  # noqa: F401  S4 工具调用审计
from app.models.alert_disposal import AlertDisposal  # noqa: F401  S3 告警处置
from app.models.persona_config import PersonaConfig  # noqa: F401  S5 persona 配置覆盖
from app.models.permission import RolePermission  # noqa: F401  RBAC 角色权限覆盖
from app.models.favorite import Favorite  # noqa: F401  个人收藏夹
from app.models.agent_memory import AgentMemory  # noqa: F401  N1 Agent 长期记忆


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
    ("persona_config", "fallback_key", "VARCHAR(32)"),  # S5 纯DB persona 的 fallback 映射
    ("users", "dept", "VARCHAR(64) NOT NULL DEFAULT ''"),                # RBAC 部门（文档级 ACL）
    ("users", "status", "VARCHAR(16) NOT NULL DEFAULT 'active'"),        # 账号状态（active|inactive 禁用）
    ("documents", "dept", "VARCHAR(64) NOT NULL DEFAULT ''"),            # RBAC 文档部门
    ("documents", "allowed_roles", "VARCHAR(256) NOT NULL DEFAULT ''"),  # RBAC 文档授权角色
    # 告警处置人工确认闭环（③增强）：转两票关联 + 审核留痕
    ("alert_disposal", "ticket_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("alert_disposal", "reviewer", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("alert_disposal", "review_note", "VARCHAR(500) NOT NULL DEFAULT ''"),
    ("alert_disposal", "reviewed_at", "DATETIME NULL"),
    ("alert_disposal", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("tickets", "source_ref", "VARCHAR(128) NULL"),
    ("qa_cache", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("evidence_gap", "is_deleted", "TINYINT(1) NOT NULL DEFAULT 0"),  # 证据补全逻辑删除
    # 可核验引用元数据（第一层：精确定位）
    ("chunks", "page_num", "INT NULL"),                                   # PDF 页码/幻灯片号（Word/txt 无→null）
    ("chunks", "bbox", "VARCHAR(128) NULL"),                              # JSON 串 [x0,y0,x1,y1]，前端 PDF 高亮
    ("chunks", "section_path", "VARCHAR(512) NOT NULL DEFAULT ''"),       # 层级章节路径 "3.1 免责 > 第2条"
    ("chunks", "table_header", "TEXT NOT NULL DEFAULT ''"),               # 表格类 chunk 绑定的表头
    ("chunks", "metadata_complete", "TINYINT(1) NOT NULL DEFAULT 0"),     # 元数据是否齐全（前端降级依据）
]

_INDEX_MIGRATIONS = [
    (
        "tickets",
        "uq_tickets_tenant_source_ref",
        "UNIQUE INDEX `uq_tickets_tenant_source_ref` (`tenant_id`, `source_ref`)",
    ),
    (
        "alert_disposal",
        "ix_alert_disposal_tenant_status_created",
        "INDEX `ix_alert_disposal_tenant_status_created` (`tenant_id`, `status`, `created_at`)",
    ),
    # B1：chunks (doc_id, chunk_idx) 复合索引（citation 回填/small-to-big 邻域召回按此序查）
    (
        "chunks",
        "ix_chunks_doc_idx",
        "INDEX `ix_chunks_doc_idx` (`doc_id`, `chunk_idx`)",
    ),
]


async def _ensure_columns() -> None:
    """幂等补列：逐条 ALTER，列已存在时 MySQL 报 1060，忽略即可。

    重要：原版 bare except 静默吞所有异常（连接/权限/锁），导致 schema drift
    长期不可见（如 chunks.table_header 加列失败后任务 SELECT 1054）。
    现版区分 1060/1061（幂等跳过）与其他异常（degraded 记录，让 /metrics 和日志可见）。
    """
    from app.core.obs import degraded
    async with engine.begin() as conn:
        for table, col, typedef in _COLUMN_MIGRATIONS:
            try:
                await conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {typedef}"))
            except Exception as e:
                msg = str(e)
                # MySQL 1060 "Duplicate column name" = 列已存在，幂等跳过
                if "1060" in msg or "Duplicate column" in msg or "already exists" in msg.lower():
                    continue
                # 其他异常（权限/锁/连接/语法）→ degraded 让 drift 可见，不再静默吞
                degraded("init_db_column_migration", e, f"{table}.{col}")
        for table, _name, definition in _INDEX_MIGRATIONS:
            try:
                await conn.execute(text(f"ALTER TABLE `{table}` ADD {definition}"))
            except Exception as e:
                msg = str(e)
                # MySQL 1061 "Duplicate key name" = 索引已存在，幂等跳过
                if "1061" in msg or "Duplicate key" in msg or "already exists" in msg.lower():
                    continue
                degraded("init_db_index_migration", e, f"{table}.{_name}")


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
