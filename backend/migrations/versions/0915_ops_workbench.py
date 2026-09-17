"""运维快照及历史遥测；兼容启动 create_all 已建表的数据库。"""

from alembic import op
import sqlalchemy as sa

revision = "0915_ops_workbench"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("ops_snapshot"):
        op.create_table(
            "ops_snapshot",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("creator", sa.String(64), nullable=False),
            sa.Column("audit_json", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_ops_snapshot_tenant_id", "ops_snapshot", ["tenant_id"])
        op.create_index("ix_ops_snapshot_kind", "ops_snapshot", ["kind"])
    if not sa.inspect(bind).has_table("ops_telemetry_point"):
        op.create_table(
            "ops_telemetry_point",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("device_id", sa.String(128), nullable=False),
            sa.Column("metric", sa.String(64), nullable=False),
            sa.Column("unit", sa.String(24), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("quality", sa.String(16), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("imported_by", sa.String(64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "source",
                "device_id",
                "metric",
                "observed_at",
                name="uq_ops_point",
            ),
        )
        op.create_index(
            "ix_ops_point_range",
            "ops_telemetry_point",
            ["tenant_id", "device_id", "metric", "observed_at"],
        )


def downgrade():
    # 有业务证据的表不自动销毁；回滚版本只需关闭功能开关。
    raise RuntimeError(
        "此迁移含审计/遥测数据；请先备份并人工决定是否删除，禁止自动降级删表"
    )
