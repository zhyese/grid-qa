"""Agent 工具调用审计表：agent 每次工具调用记一条（谁/何时/哪个 persona/调了啥/结果）。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentToolCall(Base):
    __tablename__ = "agent_tool_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    persona: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    iter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    args_json: Mapped[str] = mapped_column(Text, default="")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(64), default="")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
