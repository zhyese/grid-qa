"""add evidence_gap

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evidence_gap",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("original_answer", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=True, server_default="medium"),
        sa.Column("grade", sa.String(length=16), nullable=True, server_default=""),
        sa.Column("crag_action", sa.String(length=16), nullable=True, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=True, server_default="auto"),
        sa.Column("status", sa.String(length=16), nullable=True, server_default="pending"),
        sa.Column("ai_draft", sa.Text(), nullable=True),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("synced_doc_id", sa.String(length=64), nullable=True, server_default=""),
        sa.Column("synced_cache", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("tenant", sa.String(length=32), nullable=True, server_default="default"),
        sa.Column("operator", sa.String(length=64), nullable=True, server_default=""),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_gap_ts", "evidence_gap", ["ts"])
    op.create_index("ix_evidence_gap_status", "evidence_gap", ["status"])


def downgrade():
    op.drop_index("ix_evidence_gap_status", table_name="evidence_gap")
    op.drop_index("ix_evidence_gap_ts", table_name="evidence_gap")
    op.drop_table("evidence_gap")
