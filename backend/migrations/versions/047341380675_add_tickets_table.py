"""add tickets table

Revision ID: 047341380675
Revises: ea80f9db252e
Create Date: 2026-07-05 03:06:14.108196

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '047341380675'
down_revision: Union[str, Sequence[str], None] = 'ea80f9db252e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create tickets table only."""
    op.create_table('tickets',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=True),
    sa.Column('ticket_type', sa.Enum('OPERATION', 'WORK', name='tickettype'), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'PENDING_REVIEW', 'REVIEWED', 'ISSUED', 'IN_EXECUTION', 'COMPLETED', 'ARCHIVED', 'REJECTED', name='ticketstatus'), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('task', sa.Text(), nullable=True),
    sa.Column('device', sa.String(length=200), nullable=True),
    sa.Column('location', sa.String(length=200), nullable=True),
    sa.Column('steps', sa.Text(), nullable=True),
    sa.Column('safety_measures', sa.Text(), nullable=True),
    sa.Column('risks', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('creator', sa.String(length=64), nullable=True),
    sa.Column('reviewer', sa.String(length=64), nullable=True),
    sa.Column('issuer', sa.String(length=64), nullable=True),
    sa.Column('executor', sa.String(length=64), nullable=True),
    sa.Column('supervisor', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('issued_at', sa.DateTime(), nullable=True),
    sa.Column('executed_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('review_score', sa.Integer(), nullable=True),
    sa.Column('review_comment', sa.Text(), nullable=True),
    sa.Column('audit_report', sa.Text(), nullable=True),
    sa.Column('execution_log', sa.Text(), nullable=True),
    sa.Column('deviation', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('is_deleted', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tickets_tenant_id'), 'tickets', ['tenant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema: drop tickets table."""
    op.drop_index(op.f('ix_tickets_tenant_id'), table_name='tickets')
    op.drop_table('tickets')