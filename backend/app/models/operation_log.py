"""操作日志模型。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    operate_user: Mapped[str] = mapped_column(String(64), nullable=False)
    operate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    operate_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
