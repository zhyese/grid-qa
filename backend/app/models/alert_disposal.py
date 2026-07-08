"""告警自动处置表：告警 → ALERT_PERSONA 分析 → 诊断/处置/操作票草案。"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AlertDisposal(Base):
    __tablename__ = "alert_disposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")           # 告警原文摘要
    diagnosis_json: Mapped[str] = mapped_column(Text, default="")     # ALERT_PERSONA 输出 JSON
    handling: Mapped[str] = mapped_column(Text, default="")
    ticket_draft_json: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/disposed
    source: Mapped[str] = mapped_column(String(16), default="manual")  # webhook/manual
