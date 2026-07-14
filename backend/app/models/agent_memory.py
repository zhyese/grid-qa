"""N1 Agent 长期记忆表：存储从对话中抽取的结构化事实（用户偏好/诊断结论/待确认项）。

软删除：deleted_at 非空 = 已删除（recall 查询 WHERE deleted_at IS NULL）。
容量管理：单用户 MEMORY_CAPACITY(500) 条上限，超出时 consolidate 阶段淘汰低权重。
时间衰减：decay() 定时任务对 90/180 天未命中记忆降权。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentMemory(Base):
    """Agent 长期记忆条目（审计 + 软删除 + 容量管理）。"""

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # UUID，Milvus pk 对齐
    user_id: Mapped[str] = mapped_column(String(64), index=True)  # 用户名（scope=user 时）
    scope: Mapped[str] = mapped_column(String(16), default="user", index=True)  # user | device
    fact_text: Mapped[str] = mapped_column(Text, default="")  # 记忆文本（如"用户负责110kV城东站"）
    entity: Mapped[str] = mapped_column(String(128), default="")  # 关联实体名（如"1号主变"）
    category: Mapped[str] = mapped_column(String(32), default="preference")  # preference|diagnosis|pending
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # 权重（衰减/命中更新）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_hit_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())  # 最后命中时间
    hit_count: Mapped[int] = mapped_column(Integer, default=0)  # 命中次数
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 软删除（非NULL=已删除）
