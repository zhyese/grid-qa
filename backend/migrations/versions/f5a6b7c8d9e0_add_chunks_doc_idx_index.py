"""add chunks (doc_id, chunk_idx) composite index

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-19

B1 数据链路基础设施：chunks 表加 (doc_id, chunk_idx) 复合索引。
citation_index 回填 + small-to-big 邻域召回均按 (doc_id, chunk_idx) 序查；
原仅有 (doc_id, parent_idx) → 全表扫。
开发期主路径走 init_db._INDEX_MIGRATIONS（CREATE INDEX 幂等）；本迁移为 Alembic 路径留痕。
"""
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_chunks_doc_idx", "chunks", ["doc_id", "chunk_idx"], unique=False,
    )


def downgrade():
    op.drop_index("ix_chunks_doc_idx", table_name="chunks")
