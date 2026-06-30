"""文档版本历史（规程换版可回滚）。

同名文档重新上传 → 旧版归档到本表；回滚时把指定版本的 MinIO 对象恢复为当前，重新解析。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    doc_id: Mapped[str] = mapped_column(String(64), ForeignKey("documents.id"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    minio_object: Mapped[str] = mapped_column(String(512), nullable=False)  # 该版本的 MinIO 对象 key
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
