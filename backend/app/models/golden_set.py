"""F1 数据资产与标注工作台：golden 集资产化（版本 + 标注态 + 哈希链留痕）。

把 golden 集从"单个 JSON 文件 + 一个回流按钮"升级为带版本/标签/批量导入/回归联动的
数据资产层。运行时评测（eval_retrieval / eval_matrix）仍读 `backend/data/golden_qa.json`，
由本模块的 publish 动作把「已标注通过」的条目快照写回该文件，保持评测链路零改动。

字段对齐调研报告 v3 F1：difficulty / category / device_tags / expected_sources / label_status。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GoldenCase(Base):
    """单条 golden 用例（资产主表，软删除）。"""
    __tablename__ = "golden_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "query", name="uq_golden_tenant_query"),
        Index("ix_golden_tenant_status", "tenant_id", "label_status"),
        Index("ix_golden_tenant_category", "tenant_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    # 注意：必须是 VARCHAR 而非 TEXT —— MySQL 不允许在 TEXT/BLOB 列上建唯一约束
    # （1170 "BLOB/TEXT column used in key specification without a key length"）。
    # 单测走 sqlite 不会暴露此问题，只有真实 MySQL 建表才会炸。
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    expect_json: Mapped[str] = mapped_column(Text, default="[]")            # JSON 数组：期望命中文档关键词
    relevant_docs_json: Mapped[str] = mapped_column(Text, default="{}")     # JSON 对象：{docName: 1..3} 分级标注
    category: Mapped[str] = mapped_column(String(32), default="")           # 变电/配电/输电/...
    difficulty: Mapped[str] = mapped_column(String(16), default="")         # easy|medium|hard
    device_tags: Mapped[str] = mapped_column(String(256), default="")       # 逗号分隔设备标签
    expected_sources_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组：期望来源文档名
    # label_status: pending(待标注) | labeled(已标注) | verified(已复核通过) | rejected(弃用)
    label_status: Mapped[str] = mapped_column(String(16), default="pending")
    source: Mapped[str] = mapped_column(String(32), default="manual")       # manual|feedback|import|quality_event
    note: Mapped[str] = mapped_column(String(500), default="")
    is_deleted: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class GoldenSetVersion(Base):
    """golden 集发布版本（哈希链留痕，防篡改可追溯）。"""
    __tablename__ = "golden_set_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_golden_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    version: Mapped[int] = mapped_column(Integer, default=1)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    # 全量快照可能到数百 KB（500 条 × 数百字节），MySQL TEXT 上限 64KB 会截断 → 显式 MEDIUMTEXT(16MB)
    snapshot_json: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(500), default="")
    published: Mapped[int] = mapped_column(Integer, default=1)       # 1=已写回 golden_qa.json
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
