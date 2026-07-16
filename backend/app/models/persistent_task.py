"""数据库持久化任务队列模型。

任务执行采用至少一次语义：worker 先持久化领取信息，再执行 handler；进程异常后
由 stale-task 回收逻辑重新入队。业务 handler 因此仍应使用 ``idempotency_key``
或自身业务唯一键保证副作用幂等。
"""
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PersistentTask(Base):
    """可重试、可审计的数据库任务。"""

    __tablename__ = "persistent_tasks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_type", "idempotency_key", name="uq_pt_idem"
        ),
        Index("ix_pt_claim", "queue_name", "status", "run_after", "priority"),
        Index("ix_pt_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    queue_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default"
    )
    task_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # queued / running / succeeded / failed / dead
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(191), nullable=True
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    causation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    locked_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
