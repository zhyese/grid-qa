"""Agentic 历史快照独立表；不修改已有 messages 表结构。"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgenticAnswer(Base):
    __tablename__ = "agentic_answers"

    message_id: Mapped[str] = mapped_column(String(64), ForeignKey("messages.id"), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.id"), index=True, nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
