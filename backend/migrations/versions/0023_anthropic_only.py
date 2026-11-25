"""Anthropic API only (owner's decision, 2026-11-25): remove the other AI connectors.

Deletes every AI provider whose kind is not ``anthropic`` (Qwen, OpenAI, Ollama and other OpenAI-compatible
endpoints), together with its encrypted API key, and removes those providers from the per-task routes. Single Active AI
references to them are cleared by their foreign keys (ON DELETE SET NULL). The usage ledger keeps its history (it
stores provider names, not references).

Rollback: ``alembic downgrade 0022`` is a no-op. Deleted providers and their keys cannot be restored; add them again
only after reinstating their adapter.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-25
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    gone = {r[0] for r in conn.execute(sa.text("select id from ai_providers where kind <> 'anthropic'"))}
    if not gone:
        return
    for task, chain in conn.execute(sa.text("select task, chain from ai_task_routes")).all():
        chain = chain if isinstance(chain, list) else json.loads(chain or "[]")
        kept = [h for h in chain if h.get("provider_id") not in gone]
        if kept != chain:
            conn.execute(sa.text("update ai_task_routes set chain = cast(:c as jsonb) where task = :t"),
                         {"c": json.dumps(kept), "t": task})
    conn.execute(sa.text("delete from ai_providers where kind <> 'anthropic'"))


def downgrade() -> None:
    pass
