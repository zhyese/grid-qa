"""证据补全表：medium/refused 的问答收集 + 人工兜底回流。

状态机：pending(收集) → ai_drafted(AI续写) → synced(确认+入库) / ignored(忽略)。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EvidenceGap(Base):
    __tablename__ = "evidence_gap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)              # 归一化 nq
    original_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[str] = mapped_column(String(16), default="medium")  # medium|refused
    grade: Mapped[str] = mapped_column(String(16), default="")
    crag_action: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(16), default="auto")       # auto|manual
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|ai_drafted|synced|ignored
    ai_draft: Mapped[str] = mapped_column(Text, default="")
    final_answer: Mapped[str] = mapped_column(Text, default="")
    synced_doc_id: Mapped[str] = mapped_column(String(64), default="")
    synced_cache: Mapped[int] = mapped_column(Integer, default=0)
    tenant: Mapped[str] = mapped_column(String(32), default="default")
    operator: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
