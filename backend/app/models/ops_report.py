"""N7 运维报告智能生成模型。

ops_reports 存一次报告生成的全生命周期：
- params        生成入参快照（时间范围/站点/设备等）
- data_snapshot 聚合的数据事实（交接班/遥测/两票/告警/预测，供追溯与免 LLM 复核）
- sections      LLM 产出的分节内容 [{title, content}]
- content_md    拼装后的完整 Markdown（前端渲染 / Word 导出源）
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OpsReport(Base):
    __tablename__ = "ops_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    title: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # shift(交接班) | weekly(周报) | fault(故障分析) | device(设备健康)
    report_type: Mapped[str] = mapped_column(String(16), default="shift", index=True)
    # generating | done | failed
    status: Mapped[str] = mapped_column(String(16), default="generating", index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    data_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    sections: Mapped[list] = mapped_column(JSON, default=list)
    content_md: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")  # 报告一句话摘要（列表页）
    error: Mapped[str] = mapped_column(Text, default="")
    # LLM 元信息：{model, promptChars, fallback?}——LLM 失败走模板拼接时 fallback=true
    llm_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
