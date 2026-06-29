"""文档模型（源文档元数据；向量数据在 Milvus）。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    doc_name: Mapped[str] = mapped_column(String(256), nullable=False)          # 原文件名
    doc_type: Mapped[str] = mapped_column(String(64), default="运维手册")        # 文档分类
    minio_object: Mapped[str] = mapped_column(String(512), nullable=False)      # MinIO 对象 key
    file_size: Mapped[int] = mapped_column(Integer, default=0)                  # 字节
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|parsed|vectorized
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    upload_user: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
