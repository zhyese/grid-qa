"""add rewrite_event

Revision ID: a1b2c3d4e5f6
Revises: 047341380675
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "047341380675"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rewrite_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("rewritten_query", sa.Text(), nullable=False),
        sa.Column("improved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orig_score", sa.Float(), nullable=True),
        sa.Column("new_score", sa.Float(), nullable=True),
        sa.Column("cached", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("route", sa.String(length=16), nullable=True, server_default="hybrid"),
        sa.Column("tenant", sa.String(length=32), nullable=True, server_default="default"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rewrite_event_ts", "rewrite_event", ["ts"])
    op.create_index("ix_rewrite_event_strategy", "rewrite_event", ["strategy"])


def downgrade():
    op.drop_index("ix_rewrite_event_strategy", table_name="rewrite_event")
    op.drop_index("ix_rewrite_event_ts", table_name="rewrite_event")
    op.drop_table("rewrite_event")
