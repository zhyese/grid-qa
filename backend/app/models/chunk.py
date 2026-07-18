"""文档分块模型（结构感知：父子分块 + 类型/章节元信息）。"""
import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
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
    # ===== 可核验引用元数据（第一层：精确定位）=====
    page_num: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 页码/幻灯片号（PDF 有，Word/txt 无→null）
    bbox: Mapped[str | None] = mapped_column(String(128), nullable=True)  # JSON 串 [x0,y0,x1,y1]，前端 PDF 高亮
    section_path: Mapped[str] = mapped_column(String(512), default="")  # 层级章节路径 "3.1 免责 > 第2条"
    table_header: Mapped[str] = mapped_column(Text, default="")  # 表格类 chunk 绑定的表头（防数值丢上下文）
    metadata_complete: Mapped[bool] = mapped_column(Boolean, default=False)  # 元数据是否齐全（前端降级依据）

    __table_args__ = (Index("ix_chunks_doc_parent", "doc_id", "parent_idx"),)

    def __init__(self, **kwargs):
        # 构造期默认值：declarative_base 的 mapped_column.default 仅在 flush 时应用，
        # 旧调用方不传新字段时这里补齐 "" / False，保证读取属性时即得到向后兼容的语义默认。
        kwargs.setdefault("section_path", "")
        kwargs.setdefault("table_header", "")
        kwargs.setdefault("metadata_complete", False)
        super().__init__(**kwargs)
