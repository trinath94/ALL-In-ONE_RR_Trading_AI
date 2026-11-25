"""Claude Code + Anthropic API integration (additive).

* ``agent_tasks``: the task kind (``operator``: existing safe operator workflows; ``claude_code``: a Claude Agent SDK
  development task in an isolated git workspace; ``ai``: an Anthropic API analysis), live progress events, the Claude
  Code session id (resume), the workspace branch, cancellation request, start time and usage (tokens, cost).
* ``ai_control``: Claude Code settings (off by default; model empty = the engine's configured Claude model; per-task
  budget and turn limits; the test command the engine runs itself) and the budget alert threshold.
* ``ai_usage``: prompt-cache read/write tokens.

Rollback: ``alembic downgrade 0021`` drops only these columns.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

TASK_COLS = ('kind', 'progress', 'session_id', 'branch', 'cancel_requested', 'started_at', 'usage')
CONTROL_COLS = ('claude_code_enabled', 'claude_code_model', 'claude_code_max_budget_usd', 'claude_code_max_turns',
                'claude_code_test_command', 'budget_alert_pct')


def upgrade() -> None:
    op.add_column('agent_tasks', sa.Column('kind', sa.String(16), nullable=False, server_default='operator'))
    op.add_column('agent_tasks', sa.Column('progress', postgresql.JSONB(), nullable=False, server_default='[]'))
    op.add_column('agent_tasks', sa.Column('session_id', sa.String(64), nullable=True))
    op.add_column('agent_tasks', sa.Column('branch', sa.String(128), nullable=True))
    op.add_column('agent_tasks', sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('agent_tasks', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_tasks', sa.Column('usage', postgresql.JSONB(), nullable=False, server_default='{}'))
    op.create_index('ix_agent_tasks_kind_status', 'agent_tasks', ['kind', 'status'])
    op.add_column('ai_control', sa.Column('claude_code_enabled', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('ai_control', sa.Column('claude_code_model', sa.String(96), nullable=False, server_default=''))
    op.add_column('ai_control', sa.Column('claude_code_max_budget_usd', sa.Float(), nullable=False, server_default='2'))
    op.add_column('ai_control', sa.Column('claude_code_max_turns', sa.Integer(), nullable=False, server_default='40'))
    op.add_column('ai_control', sa.Column('claude_code_test_command', sa.String(255), nullable=False,
                                          server_default='python -m pytest -x -q -p no:cacheprovider --ignore=tests/test_mt5_agent.py'))
    op.add_column('ai_control', sa.Column('budget_alert_pct', sa.Integer(), nullable=False, server_default='80'))
    op.add_column('ai_usage', sa.Column('cache_read_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('ai_usage', sa.Column('cache_write_tokens', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('ai_usage', 'cache_write_tokens')
    op.drop_column('ai_usage', 'cache_read_tokens')
    for c in CONTROL_COLS:
        op.drop_column('ai_control', c)
    op.drop_index('ix_agent_tasks_kind_status', 'agent_tasks')
    for c in TASK_COLS:
        op.drop_column('agent_tasks', c)
