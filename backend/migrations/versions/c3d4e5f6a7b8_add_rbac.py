"""add rbac (user.dept, document.dept/allowed_roles, role_permission table)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-12

RBAC 细粒度权限 + 文档级 ACL（设计见 docs/superpowers/specs/2026-07-01-rbac-acl-design.md）。
开发期主路径走 init_db._COLUMN_MIGRATIONS + create_all；本迁移为生产/Alembic 路径留痕，两条路径等价。
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    # User 加 dept（部门，文档级 ACL 用）
    op.add_column("users", sa.Column("dept", sa.String(length=64), nullable=False, server_default=""))
    op.create_index("ix_users_dept", "users", ["dept"])
    # Document 加 dept + allowed_roles（部门 + 角色级授权；空 dept=公开，空 allowed_roles=部门内全员可读）
    op.add_column("documents", sa.Column("dept", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("documents", sa.Column("allowed_roles", sa.String(length=256), nullable=False, server_default=""))
    op.create_index("ix_documents_dept", "documents", ["dept"])
    # role_permission 覆盖表（首版空表，走 code 默认映射）
    op.create_table(
        "role_permission",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_role_permission_role", "role_permission", ["role"])


def downgrade():
    op.drop_index("ix_role_permission_role", table_name="role_permission")
    op.drop_table("role_permission")
    op.drop_index("ix_documents_dept", table_name="documents")
    op.drop_column("documents", "allowed_roles")
    op.drop_column("documents", "dept")
    op.drop_index("ix_users_dept", table_name="users")
    op.drop_column("users", "dept")
