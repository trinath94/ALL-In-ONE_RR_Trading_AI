"""Shared token issuing: hashes only, audited, short CLI lifetime, and a token from the CLI registers a connector."""
import json

import pytest
from sqlalchemy import select

from app.db.models import AuditLog
from app.db.models_agent import MT5AgentToken
from app.db.session import session_scope
from app.services import agent_tokens
from tests.test_mt5_agent import engine, make_manager  # noqa: F401 (fixture)


def test_cli_issues_short_lived_audited_hashed_token(db_clean, capsys):
    assert agent_tokens.main(["--label", "desktop", "--ttl-minutes", "10"]) == 0
    out = json.loads(capsys.readouterr().out)
    tok = out["token"]
    with session_scope() as db:
        row = db.scalar(select(MT5AgentToken))
        assert row.token_hash != tok and len(row.token_hash) == 64 and row.label == "local:desktop"
        assert (row.expires_at - row.created_at).total_seconds() <= 601
        assert db.scalar(select(AuditLog).where(AuditLog.action == "MT5_AGENT_TOKEN_CREATED")).ip == "local-supervisor"


@pytest.mark.parametrize("ttl", ["0", "61", "-5"])
def test_cli_rejects_out_of_range_lifetime(db_clean, ttl):
    assert agent_tokens.main(["--label", "x", "--ttl-minutes", ttl]) == 2


def test_cli_token_registers_a_connector_once(engine, tmp_path, capsys):
    agent_tokens.main(["--label", "pc"])
    tok = json.loads(capsys.readouterr().out)["token"]
    m, *_ = make_manager(tmp_path)
    assert m.register(engine, tok)["agent_id"]
    m2, *_ = make_manager(tmp_path / "b")
    with pytest.raises(Exception, match="already used"):
        m2.register(engine, tok)
