"""Multi-account execution (additive): per-account reconciliation state and whether the kill switch's position policy
was applied to the account. Both start false, so every additional execution account is reconciled before its first
entry. The active account keeps using trading_state.startup_checks_passed as before.

Rollback: ``alembic downgrade 0016`` drops only these columns.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trading_accounts', sa.Column('reconciled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('trading_accounts', sa.Column('emergency_applied', sa.Boolean(), nullable=False,
                                                server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('trading_accounts', 'emergency_applied')
    op.drop_column('trading_accounts', 'reconciled')
