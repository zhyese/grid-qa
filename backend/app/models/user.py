"""用户模型。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _gen_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_gen_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="operator", nullable=False)  # admin | operator
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)  # 多租户隔离
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
