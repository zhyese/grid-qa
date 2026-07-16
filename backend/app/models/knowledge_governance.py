"""知识时效与冲突治理模型。

治理元数据旁挂在 documents 表之外，避免改变现有上传、解析、检索链路。
扫描只生成潜在问题；问题状态必须经过人工审核，审核动作单独留痕。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class KnowledgeDocumentMetadata(Base):
    """规程/知识文档的治理元数据（一份文档一条）。"""

    __tablename__ = "knowledge_document_metadata"

    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    applicable_region: Mapped[str] = mapped_column(String(256), default="")
    effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False)
    review_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    version_label: Mapped[str] = mapped_column(String(64), default="")
    version_status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeGovernanceIssue(Base):
    """扫描产生的治理问题；不直接修改源文档或元数据。"""

    __tablename__ = "knowledge_governance_issue"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_doc_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    reviewer: Mapped[str] = mapped_column(String(64), default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_kg_issue_tenant_fingerprint"),
        Index("ix_kg_issue_tenant_status_type", "tenant_id", "status", "issue_type"),
    )


class KnowledgeGovernanceReview(Base):
    """治理问题人工审核流水，保留每次状态变更的审计证据。"""

    __tablename__ = "knowledge_governance_review"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    issue_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_governance_issue.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    from_status: Mapped[str] = mapped_column(String(16), default="open")
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
