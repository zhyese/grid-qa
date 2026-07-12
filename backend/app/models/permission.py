"""RBAC 角色-权限覆盖表（DB 覆盖 code 默认映射用）。

首版空表，全走 app.core.permissions.ROLE_PERMISSIONS（code 默认）。
admin 在 UI 动态调整某角色权限时写这里；has_perm 查询时合并 code 默认 + 本表覆盖。
spec 范围外不做 UI 编辑（YAGNI），但表先建好，后续接入零迁移。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RolePermission(Base):
    __tablename__ = "role_permission"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    role: Mapped[str] = mapped_column(String(16), index=True, nullable=False)    # editor | operator | auditor（admin 全权不入表）
    permission: Mapped[str] = mapped_column(String(64), nullable=False)          # doc:read / qa:answer / ...
    granted: Mapped[bool] = mapped_column(Boolean, default=True)                  # True=授予 False=撤销（覆盖默认）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
