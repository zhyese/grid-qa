"""运维工作台：证据快照与只读遥测历史，不修改现有业务表。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OpsSnapshot(Base):
    __tablename__ = "ops_snapshot"
    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    creator: Mapped[str] = mapped_column(String(64))
    audit_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TelemetryPoint(Base):
    __tablename__ = "ops_telemetry_point"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "device_id",
            "metric",
            "observed_at",
            name="uq_ops_point",
        ),
        Index("ix_ops_point_range", "tenant_id", "device_id", "metric", "observed_at"),
    )
    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(24))
    value: Mapped[float] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    imported_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
