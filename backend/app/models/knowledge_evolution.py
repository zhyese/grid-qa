"""知识库自进化闭环模型：dislike 聚类 → 盲区 → LLM 草稿 → 审核回流。

草稿不直接写知识库；必须经人工审核，approved 后才回流 Milvus（source_type=ai_evolution
打标 + quality_score 降权），并可撤回（withdrawn 删 chunk + Milvus delete）。
复刻 KnowledgeGovernanceIssue 的「扫描产出 → 人工审核留痕」范式。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class KnowledgeEvolutionDraft(Base):
    """自进化草稿：聚类盲区产出的增量知识 chunk 草稿，待人工审核回流。"""

    __tablename__ = "knowledge_evolution_draft"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    cluster_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    representative_query: Mapped[str] = mapped_column(String(500), default="")
    member_queries_json: Mapped[str] = mapped_column(Text, default="[]")        # 簇内 dislike 原始问题
    gap_evidence_json: Mapped[str] = mapped_column(Text, default="{}")          # {top1_score, hit_doc_ids, confidence}
    source_doc_ids_json: Mapped[str] = mapped_column(Text, default="[]")        # 草稿参考的规程文档
    draft_title: Mapped[str] = mapped_column(String(256), default="")
    draft_content: Mapped[str] = mapped_column(Text, default="")
    # draft|approved|indexed|rejected|withdrawn
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    chunk_id: Mapped[str] = mapped_column(String(64), default="")               # 回流后 Chunk.id（撤回入口）
    quality_score: Mapped[float] = mapped_column(Float, default=0.6)            # AI<人工1.0，检索降权
    model_type: Mapped[str] = mapped_column(String(32), default="")
    reviewer: Mapped[str] = mapped_column(String(64), default="")
    review_note: Mapped[str] = mapped_column(String(500), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_evo_draft_tenant_status", "tenant_id", "status"),
    )
