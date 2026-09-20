"""通知中心模型：站内通知（落库持久 + WS 在线推送）。

- notifications 按用户存：signoff（轮到签/签批完成/驳回）、report（生成完成/失败）、
  drill（复盘完成）等类型；read_at 空=未读。
- link 存前端路由（如 /doc-collab?doc=xxx），点通知直达现场。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read", "recipient", "read_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    recipient: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # 接收用户名
    # signoff_pending | signoff_signed | signoff_rejected | report_done | report_failed |
    # drill_finished | eval_matrix_done | system
    type: Mapped[str] = mapped_column(String(32), default="system")
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(255), default="")  # 前端路由（点击直达）
    read_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 空=未读
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
