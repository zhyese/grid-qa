"""N5 故障仿真演练沙箱模型（DRILL_SANDBOX_ENABLE 门控）。

- drill_scenarios 演练剧本：注入故障 + 时间轴传播事件（propagation）+ 应尽动作清单
  （checklist，keywords 供动作打卡匹配）；
- drill_runs    演练记录：事件流由剧本时间轴 + started_at 推导（无状态推演，不存 fired
  事件），actions 存演练者操作打卡；finish 时算分（checklist 覆盖率 + 响应及时性）
  并生成 AI 复盘（LLM 失败降级模板）。
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DrillScenario(Base):
    __tablename__ = "drill_scenarios"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    station_id: Mapped[str] = mapped_column(String(64), default="")
    fault_device: Mapped[str] = mapped_column(String(128), default="")
    fault_desc: Mapped[str] = mapped_column(String(500), default="")
    # [{tOffset(sec), device, event, severity(critical|warning|info)}] 按时间轴推演
    propagation: Mapped[list] = mapped_column(JSON, default=list)
    # [{id, action, keywords:[..]}] 动作打卡：操作文本含任一 keyword 即命中
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    # easy | normal | hard
    difficulty: Mapped[str] = mapped_column(String(16), default="normal")
    enabled: Mapped[bool] = mapped_column(Integer, default=1)
    # scenario 来源：manual 手写 | fault_chain 孪生故障链生成
    source: Mapped[str] = mapped_column(String(16), default="manual")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DrillRun(Base):
    __tablename__ = "drill_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scenario_name: Mapped[str] = mapped_column(String(128), default="")
    station_id: Mapped[str] = mapped_column(String(64), default="")
    fault_device: Mapped[str] = mapped_column(String(128), default="")
    # running | finished | aborted
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    # 剧本快照（剧本后续修改不影响进行中的演练）
    propagation: Mapped[list] = mapped_column(JSON, default=list)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    # 演练者操作打卡 [{t(sec,相对开始), user, action}]
    actions: Mapped[list] = mapped_column(JSON, default=list)
    # {matched:[{checklistId, actionIdx, responseSec}], missed:[checklistId],
    #  coverage, avgResponseSec, grade}
    score: Mapped[dict] = mapped_column(JSON, default=dict)
    evaluation_md: Mapped[str] = mapped_column(Text, default="")  # AI 复盘（Markdown）
    error: Mapped[str] = mapped_column(Text, default="")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    started_by: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
