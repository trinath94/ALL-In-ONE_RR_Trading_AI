"""Single Active AI: one provider serves every AI task, with an optional, explicitly configured fallback. Disabled
providers get no requests; without fallback a failed provider leaves the task unanswered (candidates stay unreviewed
and are never auto-executed); the gateway never switches to an unconfigured provider. Plus per-provider last success
and last error, and the Claude Pro status."""
import pytest
from sqlalchemy import select

from app.agents.claude_agent import REVIEW_SCHEMA, Review
from app.ai import gateway
from app.ai.providers import ProviderError
from app.db.models import AuditLog
from app.db.models_ai import AIControl, AIProvider
from app.db.session import session_scope
from tests.test_ai_gateway import REVIEW_OK, Scripted, keys, provider, route, usage  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)


def control(**kw):
    with session_scope() as db:
        c = db.get(AIControl, 1) or AIControl(id=1)
        for k, v in kw.items():
            setattr(c, k, v)
        db.merge(c)


def run(fake):
    return gateway.run("candidate_review", "s", {}, REVIEW_SCHEMA, Review, provider_factory=fake.factory,
                       sleep=lambda s: None)


def test_off_by_default_the_per_task_routes_apply(keys):
    a, b = provider("routed"), provider("other")
    route("candidate_review", (a, "m"))
    assert [x.name for x in gateway.chain_for("candidate_review")] == ["routed"]
    with session_scope() as db:
        assert gateway.single_active_chain(db) is None


def test_single_active_overrides_every_route_and_other_providers_get_nothing(keys):
    a, b, c = provider("claude-api", kind="anthropic"), provider("openai"), provider("qwen")
    route("candidate_review", (b, "gpt"), (c, "qwen-max"))
    route("market_summary", (c, "qwen-max"))
    control(single_active=True, active_provider_id=a, active_model="claude-x")
    for task in ("candidate_review", "market_summary", "explanation"):
        assert [(x.name, x.model) for x in gateway.chain_for(task)] == [("claude-api", "claude-x")], task
    fake = Scripted({"claude-api": [REVIEW_OK], "openai": [], "qwen": []})
    out, meta = run(fake)
    assert meta["provider"] == "claude-api" and meta["model"]
    assert [u[0] for u in usage()] == ["claude-api"], "no request to any other provider"


def test_without_fallback_a_failed_provider_leaves_the_task_unanswered(keys):
    a, b = provider("active"), provider("spare")
    route("candidate_review", (a, "m"), (b, "m"))                   # the route has a spare; single mode ignores it
    control(single_active=True, active_provider_id=a, active_model="m", fallback_enabled=False)
    fake = Scripted({"active": [ProviderError("down", "connection")] * 5, "spare": [REVIEW_OK]})
    with pytest.raises(gateway.GatewayError):
        run(fake)
    assert {u[0] for u in usage()} == {"active"}, "never switched to a provider that is not configured"


def test_the_configured_fallback_is_used_and_recorded(keys):
    a, b = provider("active"), provider("backup")
    control(single_active=True, active_provider_id=a, active_model="m1", fallback_enabled=True,
            fallback_provider_id=b, fallback_model="m2")
    fake = Scripted({"active": [ProviderError("down", "connection")] * 5, "backup": [REVIEW_OK]})
    out, meta = run(fake)
    assert meta["provider"] == "backup" and meta["fallback_from"] == "active"
    assert usage()[-1][:1] == ("backup",) and usage()[-1][3] == "active"


def test_a_switched_off_active_provider_gets_no_requests(keys):
    a = provider("off")
    with session_scope() as db:
        db.get(AIProvider, a).enabled = False
    control(single_active=True, active_provider_id=a, active_model="m")
    assert gateway.chain_for("candidate_review") == [] and not gateway.configured("candidate_review")
    assert gateway.make_reviewer() is None, "no reviewer: candidates stay unreviewed, never auto-executed"


def test_consensus_routes_fall_back_to_one_provider_in_single_mode(keys):
    a, b = provider("p1"), provider("p2")
    route("candidate_review", (a, "m"), (b, "m"))
    from app.db.models_ai import AITaskRoute
    with session_scope() as db:
        db.get(AITaskRoute, "candidate_review").mode = "consensus"
    assert gateway.route_mode("candidate_review") == "consensus"
    control(single_active=True, active_provider_id=a, active_model="m")
    assert gateway.route_mode("candidate_review") == "fallback"


def test_control_api_validates_and_audits(client, keys):
    a, b = provider("first"), provider("second")
    login(client, "admin")
    r = client.get("/api/ai/control").json()
    assert r["single_active"] is False and r["effective_chain"] is None and r["claude_pro"]["available"] is False
    put = lambda body: client.put("/api/ai/control", json=body, headers=h(client))  # noqa: E731
    assert put({"single_active": True}).status_code == 422                                    # no provider
    assert put({"single_active": True, "active_provider_id": a}).status_code == 422            # no model
    assert put({"single_active": True, "active_provider_id": a, "active_model": "m", "fallback_enabled": True,
                "fallback_provider_id": a, "fallback_model": "m"}).status_code == 422          # fallback = active
    r = put({"single_active": True, "active_provider_id": a, "active_model": "m1", "fallback_enabled": True,
             "fallback_provider_id": b, "fallback_model": "m2"})
    assert r.status_code == 200
    assert r.json()["effective_chain"] == [{"provider": "first", "model": "m1"}, {"provider": "second", "model": "m2"}]
    with session_scope() as db:
        db.get(AIProvider, b).enabled = False
    assert client.get("/api/ai/control").json()["effective_chain"] == [{"provider": "first", "model": "m1"}]
    with session_scope() as db:
        db.get(AIProvider, a).enabled = False
    assert put({"single_active": True, "active_provider_id": a, "active_model": "m1"}).status_code == 409
    assert put({"single_active": False}).json()["effective_chain"] is None
    with session_scope() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "AI_CONTROL_UPDATE")).all()
    login(client, "trader")
    assert put({"single_active": False}).status_code == 403


def test_providers_show_last_success_and_last_error(client, keys):
    a, b = provider("good"), provider("bad")
    route("candidate_review", (b, "m"), (a, "m"))
    fake = Scripted({"bad": [ProviderError("401 invalid key", "api")], "good": [REVIEW_OK]})
    run(fake)
    login(client, "viewer")
    rows = {p["name"]: p for p in client.get("/api/ai/providers").json()}
    assert rows["good"]["last_success_at"] and rows["good"]["last_error"] is None
    assert rows["bad"]["last_success_at"] is None and "invalid key" in rows["bad"]["last_error"]
    assert "api_key_enc" not in rows["bad"] and "sk-test" not in str(rows)
