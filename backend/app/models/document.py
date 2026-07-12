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
    equipment_tags: Mapped[str] = mapped_column(String(512), default="")  # 设备台账标签(逗号分隔，检索可按设备过滤)
    dept: Mapped[str] = mapped_column(String(64), default="", index=True)  # 部门，文档级 ACL（空=公开）
    allowed_roles: Mapped[str] = mapped_column(String(256), default="")  # 授权角色(逗号分隔，空=部门内全员可读)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)  # 多租户隔离
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
