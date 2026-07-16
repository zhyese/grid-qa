"""领域事件 Outbox 与订阅投递记录模型。"""
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
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


class DomainEvent(Base):
    """事务内写入的事件 Outbox 记录。"""

    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "event_type",
            "idempotency_key",
            name="uq_de_idem",
        ),
        Index("ix_de_dispatch", "status", "available_at", "occurred_at"),
        Index("ix_de_tenant_type", "tenant_id", "event_type", "occurred_at"),
        Index("ix_de_aggregate", "aggregate_type", "aggregate_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    event_type: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="internal")
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # pending / dispatching / published / failed / dead
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(191), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    causation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    locked_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EventDelivery(Base):
    """每个订阅者独立的投递游标，避免重试时重复执行已成功订阅者。"""

    __tablename__ = "event_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "subscriber", name="uq_ed_event_sub"),
        Index("ix_ed_status", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("domain_events.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    subscriber: Mapped[str] = mapped_column(String(128), nullable=False)
    pattern: Mapped[str] = mapped_column(String(160), nullable=False)
    # pending / running / succeeded / failed / dead
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
