"""质量事件总线模型（数据飞轮·跨闭环信号统一表）。

一次坏信号(dislike/low_faith/refused/eval_low/doc_blocked)统一入此表，
由 quality_event_bus 派发给订阅者(治理联动/证据补全/自进化/评测调参)，打通数据飞轮。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QualityEvent(Base):
    __tablename__ = "quality_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # feedback | online_eval | qa_service | retrieval_eval | governance
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # dislike | low_faith | refused | eval_low | doc_blocked | ...
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | handled | failed
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
