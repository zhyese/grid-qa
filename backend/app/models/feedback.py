"""问答反馈模型（沉淀坏 case 用于优化 + 接回评测闭环）。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Feedback(Base):
    __tablename__ = "feedbacks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(String(64), default="")
    query: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[str] = mapped_column(String(16), nullable=False)  # like | dislike
    username: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(256), default="")  # 用户反馈理由/纠错标注
    judge_supported: Mapped[float | None] = mapped_column(Float, default=None)  # LLM-judge 支撑率
    judge_halluc: Mapped[float | None] = mapped_column(Float, default=None)       # LLM-judge 幻觉率
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
