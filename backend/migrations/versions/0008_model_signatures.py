"""RR Trading One: HMAC signature of model artefacts (Phase 1 gap 7.8; additive)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('model_registry', sa.Column('artifact_hmac', sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column('model_registry', 'artifact_hmac')
