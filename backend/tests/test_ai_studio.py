"""Multi-provider AI studio (RR Trading One Phase 14, specification Part 14), on the Phase 9 gateway: provider presets
(Qwen, Ollama), model discovery from the provider, model version history (requested vs served), a client-side rate
limit with fallback, and the two remaining tasks wired: learning explanations and research summaries."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.ai import gateway
from app.ai.providers import AnthropicProvider
from app.db.models_ai import AgentRun, AIProvider, AIUsage
from app.db.models_intel import AIRecommendation, ModelRegistry
from app.db.session import session_scope
from app.services.events import get_redis
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)

EXPLAIN = {"summary": "Validated on held-out data.", "key_points": ["Brier skill 0.04 on 300 rows"],
           "limitations": ["synthetic data"], "what_would_change_it": ["real broker history"]}


@pytest.fixture(autouse=True)
def clean_rpm():
    r = get_redis()
    for k in r.scan_iter("rr:ai:rpm:*"):
        r.delete(k)
    for k in r.scan_iter("rr:ai:cooldown:*"):
        r.delete(k)
    yield


def test_the_only_preset_is_the_anthropic_api_without_inventing_models(client, keys):
    login(client, "viewer")
    p = {x["id"]: x for x in client.get("/api/ai/presets").json()}
    assert list(p) == ["anthropic"] and p["anthropic"]["key_required"]
    assert "does not include API access" in p["anthropic"]["note"]
    assert all("models" not in x for x in p.values()), "models come from the provider, not a hard-coded list"


def test_model_discovery_from_the_provider(client, keys, monkeypatch):
    class Models:
        def list(self, limit):
            return type("Page", (), {"data": [type("M", (), {"id": "claude-b"})(), type("M", (), {"id": "claude-a"})()]})()
    assert AnthropicProvider(None, 5, client=type("C", (), {"models": Models()})()).list_models() == ["claude-a", "claude-b"]

    pid = provider("Qwen")
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: type("P", (), {"list_models": lambda self: ["qwen-max"]})())
    login(client, "admin")
    assert client.get(f"/api/ai/providers/{pid}/models").json() == {"ok": True, "provider": "Qwen", "models": ["qwen-max"]}

    def broken(hop, factory):
        def fail(self):
            from app.ai.providers import ProviderError
            raise ProviderError("model list rejected HTTP 401: bad key", "api")
        return type("P", (), {"list_models": fail})()
    monkeypatch.setattr(gateway, "_provider", broken)
    r = client.get(f"/api/ai/providers/{pid}/models").json()
    assert r["ok"] is False and "401" in r["error"] and r["models"] == []
    login(client, "viewer")
    assert client.get(f"/api/ai/providers/{pid}/models").status_code == 403


def test_model_version_history_requested_vs_served(client, keys, monkeypatch):
    pid = provider("Qwen")
    login(client, "admin")
    client.put("/api/ai/routes/trade_research", json={"chain": [{"provider_id": pid, "model": "qwen-max"}]}, headers=h(client))
    served = iter(["qwen-max-2026-01-25", "qwen-max-2026-01-25", "qwen-max-2026-09-23"])

    class P:
        def complete_json(self, system, user, schema, model, max_tokens):
            from app.ai.providers import Completion
            return Completion({"status": "ok", "note": "n"}, next(served), 10, 2, 5.0)
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: P())
    from app.api.routes.ai import Ping
    for _ in range(3):
        gateway.run("trade_research", "s", {}, {}, Ping)
    hist = client.get("/api/ai/models/history").json()
    g = next(x for x in hist["models"] if x["provider"] == "Qwen")
    assert g["requested_model"] == "qwen-max" and g["versions"] == 2 and g["changed"] is True
    assert [v["model"] for v in g["served"]] == ["qwen-max-2026-01-25", "qwen-max-2026-09-23"]
    assert [v["calls"] for v in g["served"]] == [2, 1] and g["current"] == "qwen-max-2026-09-23"
    assert any(c["action"] == "AI_ROUTE_SET" and c["target"] == "trade_research" for c in hist["configuration_changes"])
    with session_scope() as db:
        assert {u.requested_model for u in db.scalars(select(AIUsage))} == {"qwen-max"}


def test_local_rate_limit_moves_to_the_next_provider_without_a_cooldown(keys):
    a, b = provider("Qwen"), provider("Local")
    with session_scope() as db:
        db.get(AIProvider, a).max_requests_per_minute = 1
    route("trade_research", (a, "qwen-max"), (b, "llama3"))
    fake = Scripted({"Qwen": [{"status": "ok", "note": "1"}], "Local": [{"status": "ok", "note": "2"}]})
    from app.api.routes.ai import Ping
    out1, m1 = gateway.run("trade_research", "s", {}, {}, Ping, provider_factory=fake.factory)
    out2, m2 = gateway.run("trade_research", "s", {}, {}, Ping, provider_factory=fake.factory)
    assert (m1["provider"], m2["provider"]) == ("Qwen", "Local") and m2["fallback_from"] == "Qwen"
    with session_scope() as db:
        rows = [(u.provider, u.status, u.attempts, u.error) for u in db.scalars(select(AIUsage).order_by(AIUsage.id))]
    assert rows[1] == ("Qwen", "rate_limited", 0, "local limit of 1 requests per minute reached")
    assert not get_redis().get("rr:ai:cooldown:Qwen"), "our own limit is not a provider failure"


def _model(db):
    m = ModelRegistry(model_key="prob_tp_before_sl", version=1, algorithm="logistic_regression", target="y",
                      status="VALIDATED", metrics={"test": {"brier_skill_score": 0.04, "n": 300}},
                      dataset_ref={"rows": 1000})
    db.add(m)
    db.flush()
    return m.id


def test_learning_explanation_through_its_own_route(client, keys, monkeypatch):
    login(client, "admin")
    with session_scope() as db:
        mid = _model(db)
    assert client.post("/api/learning/explain", json={"subject": "model", "id": mid}, headers=h(client)).status_code == 409
    pid = provider("Local", key=None)
    route("learning_explanation", (pid, "llama3"))
    fake = Scripted({"Local": [EXPLAIN]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    r = client.post("/api/learning/explain", json={"subject": "model", "id": mid}, headers=h(client))
    assert r.status_code == 200 and r.json()["summary"] == EXPLAIN["summary"]
    assert r.json()["label"] == "AI-generated explanation of recorded results; not evidence"
    sent = fake.seen[0]["user"]
    assert sent["model"] == "prob_tp_before_sl:v1" and sent["metrics"]["test"]["brier_skill_score"] == 0.04
    assert client.post("/api/learning/explain", json={"subject": "model", "id": 999999}, headers=h(client)).status_code == 404
    assert client.post("/api/learning/explain", json={"subject": "strategy", "id": 1}, headers=h(client)).status_code == 422
    with session_scope() as db:
        u = db.scalar(select(AIUsage).where(AIUsage.task == "learning_explanation"))
        assert (u.provider, u.status, u.requested_model) == ("Local", "ok", "llama3")
        assert db.scalar(select(AgentRun).where(AgentRun.agent_id == "ai_learning", AgentRun.task == "learning_explanation"))
        assert db.get(ModelRegistry, mid).status == "VALIDATED", "an explanation changes nothing"
    login(client, "viewer")
    assert client.post("/api/learning/explain", json={"subject": "model", "id": mid}, headers=h(client)).status_code == 403


def test_research_summary_is_stored_for_reading_and_strictly_validated(client, keys, monkeypatch):
    login(client, "admin")
    pid = provider("Qwen")
    route("research_summary", (pid, "qwen-max"))
    assert client.post("/api/learning/research-summary", json={"days": 7}, headers=h(client)).status_code == 409
    with session_scope() as db:
        db.add(AIRecommendation(source="trade_research", kind="TRADE_RESEARCH", subject="DEMO closed trades",
                                recommendation="- 12 trades: inconclusive"))
    good = {"headline": "Little evidence yet", "items": [{"topic": "trades", "summary": "12 trades, inconclusive",
                                                          "sources": ["recommendation 1"]}], "open_questions": ["more trades"]}
    bad = {**good, "items": [{**good["items"][0], "confidence": 0.9}]}        # a field the schema does not define
    fake = Scripted({"Qwen": [bad, good]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    r = client.post("/api/learning/research-summary", json={"days": 7}, headers=h(client))
    assert r.status_code == 502 and "invalid output" in r.text, "nested extra fields are refused"
    r = client.post("/api/learning/research-summary", json={"days": 7}, headers=h(client))
    assert r.status_code == 200 and r.json()["headline"] == "Little evidence yet"
    assert fake.seen[1]["user"]["recommendations"][0]["text"] == "- 12 trades: inconclusive"
    with session_scope() as db:
        rec = db.get(AIRecommendation, r.json()["recommendation_id"])
        assert (rec.source, rec.kind, rec.status) == ("research_summary", "RESEARCH_SUMMARY", "OPEN")
    assert {t["task"] for t in client.get("/api/ai/routes").json() if t["used_by_engine"]} == set(gateway.TASKS)
