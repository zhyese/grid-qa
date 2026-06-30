"""文档分块模型（结构感知：父子分块 + 类型/章节元信息）。"""
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    doc_id: Mapped[str] = mapped_column(String(64), ForeignKey("documents.id"), index=True, nullable=False)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    # 结构感知分块元信息（small-to-big：检索小块，召回同组大块）
    chunk_type: Mapped[str] = mapped_column(String(16), default="child")  # child|table|title
    parent_idx: Mapped[int] = mapped_column(Integer, default=0)  # 同文档内父块组号，同组拼接=父块全文
    section: Mapped[str] = mapped_column(String(256), default="")  # 所属章节/标题路径（结构化溯源）

    __table_args__ = (Index("ix_chunks_doc_parent", "doc_id", "parent_idx"),)
