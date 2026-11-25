"""Single Active AI (additive): one settings row for the operator's choice of one AI provider for every AI task, with
an optional, explicitly configured fallback. Off by default: the per-task routes keep working exactly as before.

Rollback: ``alembic downgrade 0017`` drops only this table.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_control',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('single_active', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('active_provider_id', sa.Integer(), sa.ForeignKey('ai_providers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('active_model', sa.String(96), nullable=False, server_default=''),
        sa.Column('fallback_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('fallback_provider_id', sa.Integer(), sa.ForeignKey('ai_providers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('fallback_model', sa.String(96), nullable=False, server_default=''),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('ai_control')
