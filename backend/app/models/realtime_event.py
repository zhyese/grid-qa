"""实时事件、设备身份映射与主动运维运行记录。"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


def _uuid() -> str:
    return uuid.uuid4().hex


class RealtimeDeviceMapping(Base):
    """源系统设备标识到平台规范设备标识的租户级映射。"""

    __tablename__ = "realtime_device_mapping"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "source_device_id",
            name="uq_rt_device_mapping_source",
        ),
        Index("ix_rt_device_mapping_canonical", "tenant_id", "canonical_device_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(200), default="")
    device_type: Mapped[str] = mapped_column(String(64), default="")
    station: Mapped[str] = mapped_column(String(200), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )


class RealtimeEvent(Base):
    """外部原始事件及规范化快照；event_id 在租户+源内幂等。"""

    __tablename__ = "realtime_event"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "event_id", name="uq_rt_event_idempotency",
        ),
        Index("ix_rt_event_device_time", "tenant_id", "canonical_device_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="alarm", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    source_device_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    canonical_device_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    canonical_device_name: Mapped[str] = mapped_column(String(200), default="")
    device_type: Mapped[str] = mapped_column(String(64), default="")
    station: Mapped[str] = mapped_column(String(200), default="")
    device_mapped: Mapped[bool] = mapped_column(Boolean, default=False)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    payload_json: Mapped[str] = mapped_column(Text, default="")
    normalized_json: Mapped[str] = mapped_column(Text, default="")
    processing_status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    rule_decision: Mapped[str] = mapped_column(String(16), default="pending")
    rule_reason: Mapped[str] = mapped_column(String(500), default="")
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)


class ProactiveOpsRun(Base):
    """一次主动运维建议运行。

    运行永远是只读模式，控制执行标记固定为 False；人工确认后也只允许创建两票草稿。
    """

    __tablename__ = "proactive_ops_run"
    __table_args__ = (
        UniqueConstraint("event_ref_id", name="uq_proactive_run_event"),
        Index("ix_proactive_run_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    event_ref_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    alert_disposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    task_id: Mapped[str] = mapped_column(String(64), default="")
    triggered_by: Mapped[str] = mapped_column(String(128), default="system")
    model_type: Mapped[str] = mapped_column(String(64), default="")

    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="warning")
    gate_reason: Mapped[str] = mapped_column(String(500), default="")
    diagnosis_json: Mapped[str] = mapped_column(Text, default="")
    recommendation_json: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="")
    ticket_draft_json: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")

    execution_mode: Mapped[str] = mapped_column(String(16), default="read_only")
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    control_executed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer: Mapped[str] = mapped_column(String(64), default="")
    review_note: Mapped[str] = mapped_column(String(500), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ticket_id: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )
