"""Multi-account foundation (additive): per-account name, trading switch, risk limits and eligible symbols and
strategies. Existing accounts keep today's behaviour: the active account of each mode is the one enabled for trading,
no account has its own risk limits (its mode's template applies), and every symbol and strategy stays eligible.

Rollback: ``alembic downgrade 0015`` drops only these columns.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trading_accounts', sa.Column('label', sa.String(64), nullable=False, server_default=''))
    op.add_column('trading_accounts', sa.Column('trading_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('trading_accounts', sa.Column('risk_overrides', postgresql.JSONB(), nullable=False,
                                                server_default=sa.text("'{}'::jsonb")))
    op.add_column('trading_accounts', sa.Column('allowed_symbols', postgresql.JSONB(), nullable=True))
    op.add_column('trading_accounts', sa.Column('allowed_strategies', postgresql.JSONB(), nullable=True))
    op.execute("UPDATE trading_accounts SET trading_enabled = is_active")


def downgrade() -> None:
    for c in ('allowed_strategies', 'allowed_symbols', 'risk_overrides', 'trading_enabled', 'label'):
        op.drop_column('trading_accounts', c)
