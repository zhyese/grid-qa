"""add citation meta columns to chunks

Revision ID: e4f5a6b7c8d9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-18

可核验引用体系第一层（源头元数据治理）：Chunk 加 5 字段支撑引用精确定位。
开发期主路径走 init_db._COLUMN_MIGRATIONS + create_all；本迁移为生产/Alembic 路径留痕，两条路径等价。
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chunks", sa.Column("page_num", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("bbox", sa.String(length=128), nullable=True))
    op.add_column("chunks", sa.Column("section_path", sa.String(length=512), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("table_header", sa.Text(), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("metadata_complete", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade():
    for col in ("metadata_complete", "table_header", "section_path", "bbox", "page_num"):
        op.drop_column("chunks", col)
