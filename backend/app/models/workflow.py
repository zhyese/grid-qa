"""可视化工作流编排模型（BRD §5.3.1）。

workflows 存拖拽画布的 DAG 定义（graph JSON：nodes+edges，节点带画布坐标）；
workflow_runs 存每次执行的逐节点状态/输出/耗时（前端运行对话框轮询渲染）。
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    # {nodes:[{id,type,key,label,params,x,y}], edges:[{id,source,target,branch?}]}
    graph: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Integer, default=1)  # sqlite/MySQL 通吃：0/1
    version: Mapped[int] = mapped_column(Integer, default=1)   # 每次保存 +1（审计/兼容标记）
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workflow_name: Mapped[str] = mapped_column(String(64), default="")
    workflow_version: Mapped[int] = mapped_column(Integer, default=1)
    # running | done | failed | stopped
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[str] = mapped_column(Text, default="")
    # [{nodeId,type,key,status,output,error,ms}]（status: pending/running/done/error/skipped）
    node_states: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
