"""AI provider gateway (RR Trading One Phase 9, specification Parts 9 and 14): providers, typed validation of untrusted
output, identical trading boundaries for every provider, credential redaction, retries / fallback / cooldown, the
usage ledger, encrypted write-only keys and the API."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.claude_agent import REVIEW_SCHEMA, Review
from app.ai import gateway
from app.ai.providers import Completion, OpenAICompatibleProvider, ProviderError
from app.core.config import get_settings
from app.db.models import AuditLog
from app.db.models_ai import AIProvider, AITaskRoute, AIUsage
from app.db.session import session_scope
from tests.test_api import client, h, login  # noqa: F401 (fixture)

REVIEW_OK = {"decision": "BUY", "market_structure": "bullish", "higher_timeframe_bias": "bullish", "observations": ["x"],
             "hypotheses": [], "conflicts": [], "invalidation_conditions": ["below 1.09"], "trade_management": "none",
             "explanation": "ok"}


@pytest.fixture
def keys(db_clean, monkeypatch):
    monkeypatch.setenv("RR_AI_KEYS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RR_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def provider(name, kind="openai_compatible", key="sk-test-123", price=None):
    from app.core.security import encrypt_ai_key
    with session_scope() as db:
        p = AIProvider(name=name, kind=kind, base_url="http://fake.local/v1", api_key_enc=encrypt_ai_key(key) if key else None,
                       timeout_s=10, max_retries=2, price_in_per_mtok=price, price_out_per_mtok=price)
        db.add(p)
        db.flush()
        return p.id


def route(task, *hops):
    with session_scope() as db:
        db.merge(AITaskRoute(task=task, chain=[{"provider_id": pid, "model": m} for pid, m in hops]))


class Scripted:
    """Fake provider: each call pops the next scripted outcome (dict = output, exception = failure)."""

    def __init__(self, script):
        self.script = script
        self.seen = []

    def factory(self, hop):
        outer = self

        class P:
            def complete_json(self, system, user, schema, model, max_tokens):
                outer.seen.append({"provider": hop.name, "model": model, "user": user, "key": hop.api_key})
                item = outer.script[hop.name].pop(0)
                if isinstance(item, Exception):
                    raise item
                return Completion(item, f"{model}-2026", 1000, 200, 12.0)
        return P()


def usage():
    with session_scope() as db:
        return [(u.provider, u.status, u.attempts, u.fallback_from, u.cost) for u in db.scalars(select(AIUsage).order_by(AIUsage.id))]


# ---- OpenAI-compatible adapter (real HTTP handling, mocked transport) -------------------------------------------
def oa(handler, key="sk-x"):
    return OpenAICompatibleProvider("http://fake.local/v1/", key, 5, transport=httpx.MockTransport(handler))


def test_openai_compatible_adapter_parses_and_maps_errors():
    seen = {}

    def ok(req):
        seen["auth"], seen["body"] = req.headers.get("authorization"), json.loads(req.content)
        return httpx.Response(200, json={"model": "qwen-max-2026", "choices": [{"finish_reason": "stop", "message": {
            "content": "```json\n" + json.dumps(REVIEW_OK) + "\n```"}}], "usage": {"prompt_tokens": 50, "completion_tokens": 9}})
    c = oa(ok).complete_json("sys", {"a": 1}, REVIEW_SCHEMA, "qwen-max", 1000)
    assert c.data == REVIEW_OK and c.model == "qwen-max-2026" and (c.input_tokens, c.output_tokens) == (50, 9)
    assert seen["auth"] == "Bearer sk-x" and seen["body"]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in seen["body"]["messages"][0]["content"] and seen["body"]["temperature"] == 0
    oa(ok, key=None).complete_json("s", {}, {}, "llama3", 10)
    assert seen["auth"] is None, "local servers (Ollama) need no key; none is invented"
    cases = [(httpx.Response(429, headers={"retry-after": "7"}), "rate_limited"), (httpx.Response(503), "connection"),
             (httpx.Response(401, text="bad key"), "api"),
             (httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}), "truncated"),
             (httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}), "invalid_output"),
             (httpx.Response(200, json={"choices": [{"message": {"content": "[1,2]"}}]}), "invalid_output")]
    for resp, kind in cases:
        with pytest.raises(ProviderError) as e:
            oa(lambda req, r=resp: r).complete_json("s", {}, {}, "m", 10)
        assert e.value.kind == kind
    with pytest.raises(ProviderError) as e:
        oa(lambda req: httpx.Response(429, headers={"retry-after": "7"})).complete_json("s", {}, {}, "m", 10)
    assert e.value.retry_after == 7.0 and e.value.retryable


# ---- routing, retries, fallback, validation, ledger ------------------------------------------------------------
def test_retry_then_fallback_with_ledger(keys):
    a, b = provider("qwen", price=None), provider("local", price=2.0)
    route("candidate_review", (a, "qwen-max"), (b, "llama3"))
    fake = Scripted({"qwen": [ProviderError("down", "connection")] * 3, "local": [REVIEW_OK]})
    sleeps = []
    out, meta = gateway.run("candidate_review", "sys", {"candidate": {}}, REVIEW_SCHEMA, Review,
                            provider_factory=fake.factory, sleep=sleeps.append)
    assert out.decision == "BUY" and meta["provider"] == "local" and meta["fallback_from"] == "qwen"
    assert len(sleeps) == 2, "max_retries=2: two backoffs, then fall back"
    assert usage() == [("qwen", "error", 3, None, None), ("local", "ok", 1, "qwen", round(1000 / 1e6 * 2 + 200 / 1e6 * 2, 6))]


def test_invalid_output_is_never_used(keys):
    a, b = provider("p1"), provider("p2")
    route("candidate_review", (a, "m1"), (b, "m2"))
    bad = {**REVIEW_OK, "lot_size": 5.0}                        # a field the schema does not define: refused
    fake = Scripted({"p1": [bad], "p2": [{**REVIEW_OK, "decision": "MAYBE"}]})
    with pytest.raises(gateway.GatewayError, match="invalid output"):
        gateway.run("candidate_review", "sys", {}, REVIEW_SCHEMA, Review, provider_factory=fake.factory, sleep=lambda s: None)
    assert [u[1] for u in usage()] == ["invalid_output", "invalid_output"]


def test_rate_limit_beyond_cap_cools_the_provider_down(keys):
    a, b = provider("busy"), provider("spare")
    route("candidate_review", (a, "m"), (b, "m"))
    fake = Scripted({"busy": [ProviderError("429", "rate_limited", retry_after=600)], "spare": [REVIEW_OK, REVIEW_OK]})
    gateway.run("candidate_review", "s", {}, REVIEW_SCHEMA, Review, provider_factory=fake.factory, sleep=lambda s: None)
    gateway.run("candidate_review", "s", {}, REVIEW_SCHEMA, Review, provider_factory=fake.factory, sleep=lambda s: None)
    st = [u[:2] for u in usage()]
    assert st == [("busy", "rate_limited"), ("spare", "ok"), ("busy", "skipped"), ("spare", "ok")]
    assert [x["provider"] for x in fake.seen] == ["busy", "spare", "spare"], "no second call to the cooling provider"


def test_credentials_never_reach_a_provider_and_each_provider_gets_only_its_own_key(keys):
    a = provider("p1", key="sk-own-key")
    route("candidate_review", (a, "m"))
    fake = Scripted({"p1": [REVIEW_OK]})
    payload = {"candidate": {"symbol": "EURUSD", "password": "broker-pw", "nested": [{"api_key": "x", "ok": 1}],
                             "account": {"login": 1001, "server": "Demo"}, "bridge_secret": "s", "authorization": "t"}}
    gateway.run("candidate_review", "s", payload, REVIEW_SCHEMA, Review, provider_factory=fake.factory, sleep=lambda s: None)
    sent = json.dumps(fake.seen[0]["user"])
    for leaked in ("broker-pw", '"api_key"', "login", "bridge_secret", "authorization"):
        assert leaked not in sent, leaked
    assert fake.seen[0]["user"]["candidate"]["nested"] == [{"ok": 1}] and fake.seen[0]["key"] == "sk-own-key"


# ---- the same trading boundaries for every provider ------------------------------------------------------------
@pytest.mark.parametrize("engine,ai,expected", [
    ({"decision": "BUY", "direction": "BUY"}, "SELL", "NO_TRADE"),               # cannot reverse
    ({"decision": "WAIT", "direction": "BUY"}, "BUY", "WAIT_FOR_CONFIRMATION"),  # cannot upgrade a non-trade
    ({"decision": "NO_TRADE", "direction": None}, "SELL", "NO_TRADE"),
    ({"decision": "BUY", "direction": "BUY"}, "BUY", "BUY"),                     # may confirm
    ({"decision": "SELL", "direction": "SELL"}, "WAIT_FOR_CONFIRMATION", "WAIT_FOR_CONFIRMATION"),  # may downgrade
])
def test_boundaries_apply_to_non_anthropic_providers(keys, engine, ai, expected, monkeypatch):
    monkeypatch.setattr("app.services.events.agent_activity", lambda *a, **k: None)
    a = provider("qwen")
    route("candidate_review", (a, "qwen-max"))
    fake = Scripted({"qwen": [{**REVIEW_OK, "decision": ai}]})
    review, meta = gateway.GatewayReviewer(fake.factory).review({**engine, "symbol": "EURUSD.r", "timeframe": "H1"})
    assert review.decision == expected and meta["model"] == "qwen/qwen-max-2026"


def test_original_behaviour_without_routes(keys, monkeypatch):
    assert gateway.make_reviewer() is None, "no route and no RR_ANTHROPIC_API_KEY: no reviewer (UNREVIEWED)"
    monkeypatch.setenv("RR_ANTHROPIC_API_KEY", "sk-ant-env")
    get_settings.cache_clear()
    hops = gateway.chain_for("candidate_review")
    assert [(x.name, x.kind, x.model) for x in hops] == [(gateway.ENV_PROVIDER, "anthropic", get_settings().claude_model)]
    assert isinstance(gateway.make_reviewer(), gateway.GatewayReviewer)


# ---- API -------------------------------------------------------------------------------------------------------
def test_provider_api_keys_write_only_and_audited_without_secrets(client, keys):
    login(client, "trader")
    assert client.post("/api/ai/providers", json={"name": "q", "kind": "openai_compatible"}, headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post("/api/ai/providers", json={"name": "q", "kind": "openai_compatible"},
                       headers=h(client)).status_code == 422, "OpenAI-compatible needs a base URL"
    r = client.post("/api/ai/providers", json={"name": "Qwen", "kind": "openai_compatible", "api_key": "sk-secret-777",
                                               "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
                    headers=h(client))
    assert r.status_code == 200 and r.json()["has_key"] is True and "sk-secret-777" not in r.text
    pid = r.json()["id"]
    assert "sk-secret-777" not in client.get("/api/ai/providers").text
    with session_scope() as db:
        stored = db.get(AIProvider, pid).api_key_enc
        assert stored and "sk-secret-777" not in stored
        audits = " ".join(json.dumps(a.detail, default=str) for a in db.scalars(select(AuditLog)))
        assert "sk-secret-777" not in audits and "key_set" in audits
    assert client.patch(f"/api/ai/providers/{pid}", json={"api_key": ""}, headers=h(client)).json()["has_key"] is False
    r = client.put("/api/ai/routes/candidate_review", json={"chain": [{"provider_id": pid, "model": "qwen-max"}]}, headers=h(client))
    assert r.status_code == 200
    rows = {x["task"]: x for x in client.get("/api/ai/routes").json()}
    assert rows["candidate_review"]["source"] == "route" and rows["candidate_review"]["effective"][0]["provider"] == "Qwen"
    assert rows["trade_research"]["used_by_engine"] is False and rows["trade_research"]["source"] == "not configured"
    assert client.put("/api/ai/routes/unknown", json={"chain": []}, headers=h(client)).status_code == 404


def test_keys_cannot_be_stored_without_the_key_encryption_key(client, db_clean, monkeypatch):
    monkeypatch.setenv("RR_AI_KEYS_KEY", "")
    get_settings.cache_clear()
    try:
        login(client, "admin")
        r = client.post("/api/ai/providers", json={"name": "a", "kind": "anthropic", "api_key": "sk-ant"}, headers=h(client))
        assert r.status_code == 409 and "RR_AI_KEYS_KEY" in r.text
    finally:
        get_settings.cache_clear()


def test_usage_health_and_provider_test(client, keys, monkeypatch):
    a = provider("local", price=None)
    fake = Scripted({"local": [{"status": "ok", "note": "fine"}, ProviderError("refused", "api")]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    login(client, "admin")
    r = client.post(f"/api/ai/providers/{a}/test", params={"model": "llama3"}, headers=h(client)).json()
    assert r["ok"] is True and r["model"] == "llama3-2026"
    r = client.post(f"/api/ai/providers/{a}/test", params={"model": "llama3"}, headers=h(client)).json()
    assert r["ok"] is False and "refused" in r["error"]
    u = client.get("/api/ai/usage").json()
    ok = next(x for x in u["summary"] if x["status"] == "ok")
    assert ok["task"] == "diagnostics" and ok["input_tokens"] == 1000 and ok["cost"] is None and ok["cost_note"] == "no price configured"
    hl = {x["provider"]: x for x in client.get("/api/ai/health").json()}
    assert hl["local"]["state"] == "degraded" and hl["local"]["last_error"]["status"] == "error"
