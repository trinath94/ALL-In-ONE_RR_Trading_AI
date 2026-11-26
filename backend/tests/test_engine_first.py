"""Engine-first operation: the engine makes every routine decision itself; the Anthropic API is an improvement
service (owner's decision, 2026-11-26)."""
import pytest
from sqlalchemy import select

from app.ai import gateway
from app.db.models import AgentConversation, AuditLog, TradeOrder, TradingSignal
from app.db.models_ai import AIControl, AIUsage
from app.db.models_opportunity import StrategyConfigProfile
from app.db.session import session_scope
from app.learning_engine import scheduler
from app.scanner.service import scan_once
from app.services import agent, assistant, autonomy, orchestrator
from app.services import trading as tr
from app.strategies import registry
from app.workers import main as worker
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto


class NoAI:
    """A reviewer that must never be called."""
    model = "none"
    calls = 0

    def throttle_ok(self, *a):
        return True

    def review(self, candidate):
        NoAI.calls += 1
        raise AssertionError("the AI was called for a routine candidate")


def run_scan(scan, monkeypatch, policy=None, reviewer=None):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = "AI"
        if policy:
            db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                               **policy))
    scan_once(bridge, reviewer or NoAI(), worker.auto_execute_factory(bridge))
    with session_scope() as db:
        sigs = [s for s in db.scalars(select(TradingSignal)) if s.decision in ("BUY", "SELL")]
        orders = db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED")).all()
        return [dict(s.ai_review or {}) for s in sigs], orders, fake


def test_by_default_the_engine_decides_every_candidate_without_any_ai_call(scan, monkeypatch):
    NoAI.calls = 0
    reviews, orders, fake = run_scan(scan, monkeypatch)
    assert reviews and all(r.get("mode") == "ENGINE" for r in reviews if r)
    assert orders and fake.positions, "the engine's validated signal went through the Risk Engine to the broker"
    assert NoAI.calls == 0
    with session_scope() as db:
        assert db.scalar(select(AIUsage.id)) is None and db.scalar(select(AgentConversation.id)) is None


def test_selective_review_only_for_uncertain_candidates_within_the_daily_cap(scan, monkeypatch):
    class Counting(NoAI):
        def review(self, candidate):
            NoAI.calls += 1
            from app.agents.claude_agent import AgentError
            raise AgentError("unavailable", "connection")
    NoAI.calls = 0
    reviews, _, _ = run_scan(scan, monkeypatch, {"candidate_review": "selective", "review_confidence_below": 1.01,
                                                 "review_daily_cap": 1}, Counting())
    assert NoAI.calls == 1, "only one AI review a day under the cap"
    shortlisted = [r for r in reviews if r]
    assert sum(1 for r in shortlisted if r.get("mode") == "ENGINE") == len(shortlisted) - 1
    assert all("cap 1 reached" in r["note"] for r in shortlisted if r.get("mode") == "ENGINE")
    with session_scope() as db:
        sig = db.scalars(select(TradingSignal)).first()
        sig.calibrated_confidence = 0.9
        db.merge(AIControl(id=1, candidate_review="selective", review_confidence_below=0.6, review_daily_cap=100))
    with session_scope() as db:
        sig = db.scalars(select(TradingSignal)).first()
        assert tr.wants_ai_review(db, sig) == (False, "engine confident (0.90 >= 0.6)")


def test_a_rejection_by_the_ai_still_blocks_and_engine_decisions_execute():
    st = type("S", (), {"auto_trading": True, "auto_trading_since": None, "ai_mode": "AI"})()
    sig = type("G", (), {"decision": "BUY", "created_at": None, "ai_review": {"decision": "NO_TRADE"}})()
    assert tr.auto_execution_block(st, sig) is not None
    sig.ai_review = tr.engine_decided_review("candidate review off")
    assert tr.auto_execution_block(st, sig) is None
    st.ai_mode = "ENGINE_ONLY"
    assert tr.auto_execution_block(st, sig) is None


def test_the_assistant_answers_internally_unless_ai_phrasing_is_on(keys):
    pid = provider("claude")
    route("engine_assistant", (pid, "m"))
    fake = Scripted({"claude": [{"answer": "AI text", "next_steps": []}]})
    with session_scope() as db:
        out = assistant.answer(db, "engine status", provider_factory=fake.factory)
        assert out["source"] == "engine" and not fake.seen
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           assistant_ai=True))
    with session_scope() as db:
        out = assistant.answer(db, "engine status", provider_factory=fake.factory)
        assert out["source"] == "ai" and len(fake.seen) == 1


@pytest.mark.parametrize("q,target,workflow", [
    ("Reduce Anthropic token usage and move routine analysis to the internal engine.", "runtime", "autonomy"),
    ("Check whether the engine can operate without Anthropic API.", "runtime", "autonomy"),
    ("Analyze my trading history and identify weak strategies.", "runtime", "weak_strategies"),
    ("Discover new strategy candidates and validate them.", "runtime", "discovery"),
    ("Ask Anthropic to improve the existing strategy and save the proposed update.", "anthropic_improve", None),
    ("Improve the existing indicator library.", "claude_code", None),
    ("Complete the autonomous upgrade and start the internal runtime.", "runtime", "handover"),
    ("Audit the complete engine and repair all errors.", "runtime", "repair"),
])
def test_chat_commands_route_to_the_right_service(q, target, workflow):
    assert orchestrator.route(q)[0] == target
    if workflow:
        assert agent.is_command(q) and agent.workflow_for(q)[0] == workflow


def test_autonomy_report_and_independence(stack):
    with session_scope() as db:
        t = agent.start(db, "Check whether the engine can operate without Anthropic API.", None)
        r = t.report
        assert r["independence"]["can_trade_without_ai"] is True and r["independence"]["routine_dependencies"] == []
        assert "candidate_review" not in [x["task"] for x in r["autonomy"]["ai"]] if isinstance(r["autonomy"]["ai"], list) else True
        assert "Engine autonomy" in agent.describe(t)
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           candidate_review="all", assistant_ai=True))
    with session_scope() as db:
        ind = autonomy.independence(db)
        assert len(ind["routine_dependencies"]) == 2 and ind["can_trade_without_ai"] is False


def weak_code():
    return next(c for c, cls in registry.all_strategies().items() if dict(cls.params))


def test_engine_triggered_improvement_research_saves_a_versioned_proposal(keys, monkeypatch):
    code = weak_code()
    finding = {"strategy": code, "verdict": "recurring_defect_suspected", "p_value": 0.001, "detail": "12/15 losses"}
    monkeypatch.setattr("app.learning_engine.review.loss_significance",
                        lambda db, mode, c: finding if c == code else {"verdict": "normal_variance"})
    pid = provider("claude")
    route("config_proposal", (pid, "m"))
    k, v = next(iter(dict(registry.get(code).params).items()))
    answer = {"params": {k: v}, "regime": None, "reason": "tighten for the observed weakness"}
    fake = Scripted({"claude": [answer, answer]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    out = scheduler.job_improvement_research()
    assert out["weak_strategies"] == 1 and [p["strategy"] for p in out["proposals"]] == [code]
    pkg = fake.seen[0]["user"]
    assert pkg["observed_weakness"]["detail"] == "12/15 losses" and "objective" in pkg and "defaults" in pkg
    with session_scope() as db:
        p = db.scalar(select(StrategyConfigProfile).where(StrategyConfigProfile.strategy_code == code))
        assert p.source == "AI" and p.status == "PROPOSED" and p.evidence["trigger"] == "engine"
        assert p.ai_provider.startswith("claude/")
    assert scheduler.job_improvement_research()["weak_strategies"] == 0, "never twice in 7 days for one strategy"
    with session_scope() as db:
        db.merge(AIControl(id=1, auto_improvement=False))
    assert "switched off" in scheduler.job_improvement_research()["skipped"]


def test_chat_improvement_request_saves_a_proposal_for_the_named_strategy(client, keys, monkeypatch):
    code = weak_code()
    pid = provider("claude")
    route("config_proposal", (pid, "m"))
    k, v = next(iter(dict(registry.get(code).params).items()))
    fake = Scripted({"claude": [{"params": {k: v}, "regime": None, "reason": "r"}]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    login(client, "viewer")
    q = f"Ask Anthropic to improve {code} and save the proposed update"
    assert client.post("/api/assistant/ask", json={"question": q}, headers=h(client)).status_code == 403
    login(client, "admin")
    out = client.post("/api/assistant/ask", json={"question": q}, headers=h(client)).json()
    assert out["route"] == "anthropic_improve" and out["profile"]["strategy"] == code and "PROPOSED" in out["answer"]


def test_dashboard_and_settings_api(client, stack):
    login(client, "viewer")
    r = client.get("/api/ai/autonomy?days=7").json()
    assert {"internal", "ai", "claude_code", "budget", "policy", "independence", "improvements"} <= set(r)
    assert r["policy"]["candidate_review"] == "off"
    assert client.put("/api/ai/engine-first", json={"candidate_review": "all"}, headers=h(client)).status_code == 403
    login(client, "admin")
    r = client.put("/api/ai/engine-first", json={"candidate_review": "selective", "review_daily_cap": 5},
                   headers=h(client)).json()
    assert r["candidate_review"] == "selective" and r["review_daily_cap"] == 5
    assert client.put("/api/ai/engine-first", json={"candidate_review": "sometimes"}, headers=h(client)).status_code == 422
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "AI_ENGINE_FIRST_UPDATE"))
