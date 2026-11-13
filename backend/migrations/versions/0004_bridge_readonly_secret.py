"""RR Trading One: read-only bridge credential per MT5 agent (additive, nullable)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('mt5_agents', sa.Column('bridge_readonly_secret_enc', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('mt5_agents', 'bridge_readonly_secret_enc')
