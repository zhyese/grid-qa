"""Persona 配置覆盖表：admin 自助调 system_prompt/工具/参数，不发版。

DB 有 enabled 记录 → 覆盖 code persona 的可配置字段（fallback 保留 code 的，callable 不能入库）。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PersonaConfig(Base):
    __tablename__ = "persona_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)  # diagnose/qa/alert/自定义
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    allowed_tools: Mapped[str] = mapped_column(Text, default="")      # JSON 数组；空=不覆盖
    max_iter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_format: Mapped[str | None] = mapped_column(String(16), nullable=True)  # json/text
    fallback_key: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 纯DB persona 的 fallback 映射(qa/diagnose/alert/none)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
