"""Claude agent boundaries (SDK client stubbed — no network) and a full scanner pass via the bridge."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.agents.claude_agent import AgentError, ClaudeAgent, Review, enforce_boundaries
from app.db.models import AnalysisResult
from app.db.session import session_scope


class StubMessages:
    def __init__(self, payload, stop="end_turn"):
        self.payload, self.stop, self.calls = payload, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(model=kw["model"], stop_reason=self.stop,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def stub_client(payload, stop="end_turn"):
    m = StubMessages(payload, stop)
    return SimpleNamespace(beta=SimpleNamespace(messages=m)), m


REVIEW = {"decision": "BUY", "market_structure": "bullish BOS", "higher_timeframe_bias": "bullish",
          "observations": ["BOS at 1.1"], "hypotheses": ["continuation"], "conflicts": [],
          "invalidation_conditions": ["close below 1.098"], "trade_management": "BE at 1R", "explanation": "ok"}


def test_review_request_shape_and_parse(monkeypatch):
    monkeypatch.setattr("app.agents.claude_agent.agent_activity", lambda *a, **k: None)
    client, m = stub_client(REVIEW)
    agent = ClaudeAgent(client=client)
    r, meta = agent.review({"symbol": "EURUSD", "direction": "BUY", "decision": "BUY"})
    assert r.decision == "BUY"
    kw = m.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]


def test_agent_cannot_flip_or_upgrade():
    rv = Review(**{**REVIEW, "decision": "SELL"})
    assert enforce_boundaries({"direction": "BUY", "decision": "BUY"}, rv).decision == "NO_TRADE"
    rv2 = Review(**REVIEW)
    assert enforce_boundaries({"direction": "BUY", "decision": "NO_TRADE"}, rv2).decision == "NO_TRADE"
    rv3 = Review(**{**REVIEW, "decision": "NO_TRADE"})
    assert enforce_boundaries({"direction": "BUY", "decision": "BUY"}, rv3).decision == "NO_TRADE"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_and_truncation_are_errors(monkeypatch, stop):
    monkeypatch.setattr("app.agents.claude_agent.agent_activity", lambda *a, **k: None)
    client, _ = stub_client(REVIEW, stop)
    with pytest.raises(AgentError):
        ClaudeAgent(client=client).review({"direction": "BUY", "decision": "BUY"})


def test_invalid_structured_output_rejected(monkeypatch):
    monkeypatch.setattr("app.agents.claude_agent.agent_activity", lambda *a, **k: None)
    client, _ = stub_client({"decision": "MAYBE"})
    with pytest.raises(AgentError):
        ClaudeAgent(client=client).review({"direction": "BUY", "decision": "BUY"})


def test_full_scan_through_bridge(stack):
    """Scanner pulls real bars from the connector, runs all enabled strategies, stores analyses."""
    from app.scanner.service import scan_once
    from app.workers.main import startup_checks
    fake, bridge = stack
    import numpy as np
    base = fake.copy_rates_from_pos

    def varied(name, tf, start, count):   # give the fake feed some movement
        arr = base(name, tf, start, count)
        rng = np.random.default_rng(1)
        steps = np.cumsum(rng.normal(0, 0.0008, count)) * arr["close"][0]
        arr["close"] = arr["close"] + steps
        arr["open"] = np.r_[arr["close"][0], arr["close"][:-1]]
        arr["high"] = np.maximum(arr["open"], arr["close"]) + abs(rng.normal(0, 0.0004, count)) * arr["close"]
        arr["low"] = np.minimum(arr["open"], arr["close"]) - abs(rng.normal(0, 0.0004, count)) * arr["close"]
        return arr
    fake.copy_rates_from_pos = varied
    assert startup_checks(bridge)[0]
    executed = []
    res = scan_once(bridge, None, executed.append)
    assert res["analyses"] >= 2 and res["errors"] == 0
    with session_scope() as db:
        assert db.scalar(select(func.count(AnalysisResult.id))) >= 2
        a = db.scalar(select(AnalysisResult))
        assert a.regime and "score" in a.score


from tests.test_execution_e2e import stack  # noqa: E402,F401  (reuse fixture)
