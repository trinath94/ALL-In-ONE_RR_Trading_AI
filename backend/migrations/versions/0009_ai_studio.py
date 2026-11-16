"""RR Trading One Phase 14 (AI studio; additive): the model a call asked for beside the model the provider served
(model version history), and an optional per-provider request rate limit.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ai_usage', sa.Column('requested_model', sa.String(96), nullable=True))
    op.add_column('ai_providers', sa.Column('max_requests_per_minute', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_providers', 'max_requests_per_minute')
    op.drop_column('ai_usage', 'requested_model')
