"""改写事件表：每次改写记一条，供 Query 改写质量评估面板。"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RewriteEvent(Base):
    __tablename__ = "rewrite_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # rewrite|multi|hyde
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str] = mapped_column(Text, nullable=False)
    improved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)      # 0/1
    orig_score: Mapped[float] = mapped_column(Float, default=0.0)
    new_score: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[int] = mapped_column(Integer, default=0)                         # 0/1
    route: Mapped[str] = mapped_column(String(16), default="hybrid")
    tenant: Mapped[str] = mapped_column(String(32), default="default")
