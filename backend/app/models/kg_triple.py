"""知识图谱三元组模型（设备-故障-处置 关系）。

由 LLM 从运维文档分块中抽取，支撑关系图谱可视化与结构化检索。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KgTriple(Base):
    __tablename__ = "kg_triples"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)   # 主体（设备/部件/故障）
    relation: Mapped[str] = mapped_column(String(128), nullable=False)              # 关系（发生/处置/属于...）
    object: Mapped[str] = mapped_column(String(256), nullable=False, index=True)    # 客体（故障/处置/参数...）
    doc_id: Mapped[str] = mapped_column(String(64), index=True, default="")         # 来源文档 id
    doc_name: Mapped[str] = mapped_column(String(256), default="")                  # 来源文档名
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
