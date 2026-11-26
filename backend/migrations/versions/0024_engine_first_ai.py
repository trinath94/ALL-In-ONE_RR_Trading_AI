"""Engine-first operation (owner's decision, 2026-11-26): the engine makes every routine decision itself; the
Anthropic API is an improvement service.

* ``ai_control.candidate_review``: off (default, set for every installation) | selective | all. Off: no AI call per
  scan candidate; the engine's validated signal goes to the Risk Engine. Selective: only uncertain candidates (calibrated
  confidence below ``review_confidence_below``), at most ``review_daily_cap`` per day. All: the previous behaviour.
* ``ai_control.assistant_ai``: the Engine Assistant phrases answers with the AI (default off: the engine answers).
* ``ai_control.auto_improvement`` / ``improvement_daily_cap``: engine-triggered improvement research (weak strategies
  -> one AI configuration proposal each, validated by the engine before any use).
* ``strategy_config_profiles.evidence``: the research package and the engine's evidence behind a proposal.

Rollback: ``alembic downgrade 0023`` drops only these columns.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None

COLS = ('candidate_review', 'review_daily_cap', 'review_confidence_below', 'assistant_ai', 'auto_improvement',
        'improvement_daily_cap')


def upgrade() -> None:
    op.add_column('ai_control', sa.Column('candidate_review', sa.String(12), nullable=False, server_default='off'))
    op.add_column('ai_control', sa.Column('review_daily_cap', sa.Integer(), nullable=False, server_default='10'))
    op.add_column('ai_control', sa.Column('review_confidence_below', sa.Float(), nullable=False, server_default='0.6'))
    op.add_column('ai_control', sa.Column('assistant_ai', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('ai_control', sa.Column('auto_improvement', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('ai_control', sa.Column('improvement_daily_cap', sa.Integer(), nullable=False, server_default='2'))
    op.add_column('strategy_config_profiles', sa.Column('evidence', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('strategy_config_profiles', 'evidence')
    for c in COLS:
        op.drop_column('ai_control', c)
