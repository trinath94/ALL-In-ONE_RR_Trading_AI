"""Market playbooks: the AI's trading plan per symbol and market regime, saved and applied by the engine.

* ``market_playbooks``: symbol, regime, bias, allowed directions, preferred strategies, conditions to avoid, risk
  posture and size multiplier (<= 1), the reasoning, validity window, source (ai | reused), the playbook it was reused
  from, provider/model, tokens, status and graded outcomes of the trades it governed.
* ``ai_control``: playbooks on/off, validity hours, daily cap of new AI playbooks, trades needed before a playbook can
  be reused without an AI call.

Rollback: ``alembic downgrade 0024`` drops the table and the columns.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None

COLS = ('playbook_enabled', 'playbook_hours', 'playbook_daily_cap', 'playbook_reuse_min_trades')


def upgrade() -> None:
    op.create_table(
        'market_playbooks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('symbol', sa.String(64), nullable=False),
        sa.Column('regime', sa.String(32), nullable=False),
        sa.Column('bias', sa.String(8), nullable=False),
        sa.Column('allowed_directions', postgresql.JSONB(), nullable=False),
        sa.Column('preferred_strategies', postgresql.JSONB(), nullable=False),
        sa.Column('avoid', postgresql.JSONB(), nullable=False),
        sa.Column('risk_posture', sa.String(16), nullable=False),
        sa.Column('size_multiplier', sa.Float(), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=False),
        sa.Column('facts', postgresql.JSONB(), nullable=False),
        sa.Column('source', sa.String(12), nullable=False),
        sa.Column('reused_from', sa.Integer(), sa.ForeignKey('market_playbooks.id', ondelete='SET NULL'), nullable=True),
        sa.Column('ai_provider', sa.String(160), nullable=True),
        sa.Column('tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('outcomes', postgresql.JSONB(), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_playbook_symbol_status', 'market_playbooks', ['symbol', 'status'])
    op.create_index('ix_playbook_symbol_regime', 'market_playbooks', ['symbol', 'regime'])
    op.add_column('ai_control', sa.Column('playbook_enabled', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('ai_control', sa.Column('playbook_hours', sa.Integer(), nullable=False, server_default='12'))
    op.add_column('ai_control', sa.Column('playbook_daily_cap', sa.Integer(), nullable=False, server_default='12'))
    op.add_column('ai_control', sa.Column('playbook_reuse_min_trades', sa.Integer(), nullable=False,
                                          server_default='3'))


def downgrade() -> None:
    for c in COLS:
        op.drop_column('ai_control', c)
    op.drop_index('ix_playbook_symbol_regime', 'market_playbooks')
    op.drop_index('ix_playbook_symbol_status', 'market_playbooks')
    op.drop_table('market_playbooks')
