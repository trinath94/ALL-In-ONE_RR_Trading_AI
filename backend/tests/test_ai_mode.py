"""Run the engine with AI (default) or without AI ("engine-only"). Without AI the engine's own BUY/SELL signals are
auto-executed in DEMO with no AI provider configured, through the same Risk Engine and journalled execution manager.
The mode never makes an AI-rejected or unreviewed signal executable after the fact, and LIVE needs its own consent."""
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import TradeOrder, TradingSignal
from app.db.models_opportunity import Opportunity
from app.db.session import session_scope
from app.scanner.service import expire_signals, scan_once
from app.services import trading as tr
from app.workers import main as worker
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto


def set_mode(mode):
    with session_scope() as db:
        tr.get_state(db).ai_mode = mode


def test_ai_mode_defaults_to_ai_and_switches_with_audit(client):
    login(client, "trader")
    assert client.get("/api/control/state").json()["ai_mode"] == "AI"
    r = client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY"}, headers=h(client))
    assert r.status_code == 200 and r.json()["ai_mode"] == "ENGINE_ONLY"
    assert client.post("/api/control/ai-mode", json={"mode": "MAYBE"}, headers=h(client)).status_code == 422
    assert client.post("/api/control/ai-mode", json={"mode": "AI"}, headers=h(client)).json()["ai_mode"] == "AI"
    from app.db.models import AuditLog
    with session_scope() as db:
        acts = {a.action for a in db.scalars(select(AuditLog))}
    assert {"AI_MODE_ENGINE_ONLY", "AI_MODE_AI"} <= acts
    login(client, "viewer")
    assert client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY"}, headers=h(client)).status_code == 403


def test_live_engine_only_needs_trade_live_and_the_typed_phrase(client):
    with session_scope() as db:
        tr.get_state(db).active_mode = "LIVE"        # state only, for the test; no LIVE account or order is involved
    login(client, "trader")                            # trade.control without trade.live
    assert client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY", "confirm_text": "RUN WITHOUT AI"},
                       headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY"}, headers=h(client)).status_code == 428
    assert client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY", "confirm_text": "run without ai"},
                       headers=h(client)).status_code == 428
    with session_scope() as db:
        assert tr.get_state(db).ai_mode == "AI"
    assert client.post("/api/control/ai-mode", json={"mode": "ENGINE_ONLY", "confirm_text": "RUN WITHOUT AI"},
                       headers=h(client)).json()["ai_mode"] == "ENGINE_ONLY"
    login(client, "trader")                            # going back to AI never needs a phrase
    assert client.post("/api/control/ai-mode", json={"mode": "AI"}, headers=h(client)).json()["ai_mode"] == "AI"


def sig(decision="BUY", review=None, created=None):
    from datetime import UTC, datetime
    return SimpleNamespace(decision=decision, ai_review=review, created_at=created or datetime.now(UTC))


def test_the_eligibility_rule_never_crosses_modes():
    from datetime import UTC, datetime, timedelta
    since = datetime.now(UTC) - timedelta(hours=1)
    ai = SimpleNamespace(auto_trading=True, auto_trading_since=since, ai_mode="AI")
    eng = SimpleNamespace(auto_trading=True, auto_trading_since=since, ai_mode="ENGINE_ONLY")
    skipped = tr.engine_only_review()
    assert tr.auto_execution_block(eng, sig(review=skipped)) is None
    assert tr.auto_execution_block(ai, sig(review={"decision": "BUY"})) is None
    # an AI-rejected or unreviewed candidate is never made executable by switching to engine-only
    for rv in ({"decision": "NO_TRADE"}, {"decision": "UNREVIEWED"}, {"decision": "BUY"}, None):
        assert tr.auto_execution_block(eng, sig(review=rv)) is not None, rv
    # a signal produced without AI is not executed once the AI mode is back
    assert tr.auto_execution_block(ai, sig(review=skipped)) is not None
    # unchanged rules in both modes
    for st in (ai, eng):
        assert tr.auto_execution_block(st, sig("WAIT", skipped)) is not None
        assert tr.auto_execution_block(st, sig(review=skipped, created=since - timedelta(minutes=1))) is not None
        off = SimpleNamespace(**{**vars(st), "auto_trading": False})
        assert tr.auto_execution_block(off, sig(review=skipped)) is not None
    assert "does not confirm" in tr.auto_execution_block(ai, sig("BUY", {"decision": "SELL"}))


def test_engine_only_executes_in_demo_without_any_ai_provider(scan, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    ok, report = worker.startup_checks(bridge)
    assert ok, report
    enable_auto()
    set_mode("ENGINE_ONLY")
    scan_once(bridge, None, worker.auto_execute_factory(bridge))          # no AI provider at all
    with session_scope() as db:
        expire_signals(db)
    with session_scope() as db:
        assert tr.get_state(db).active_mode == "DEMO"
        orders = db.scalars(select(TradeOrder).where(TradeOrder.source == "AUTO", TradeOrder.status == "CONFIRMED")).all()
        assert orders, "an engine BUY, approved by the Risk Engine, was executed without AI"
        s = db.get(TradingSignal, orders[0].signal_id)
        assert s.status == "EXECUTED" and s.ai_review["decision"] == "SKIPPED" and s.ai_review["mode"] == "ENGINE_ONLY"
        from app.db.models import RiskEvaluation
        ev = db.scalar(select(RiskEvaluation).where(RiskEvaluation.signal_id == s.id))
        assert ev is not None and ev.approved, "the Risk Engine still decided"
        opp = db.scalar(select(Opportunity).where(Opportunity.signal_id == s.id))
        assert opp.state == "EXECUTED"
        assert any(t["stage"] == "ai_review" and t.get("result") == "SKIPPED" for t in opp.trace), opp.trace


def test_ai_mode_without_provider_still_executes_nothing(scan, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    scan_once(bridge, None, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        assert not db.scalars(select(TradeOrder)).all() and not fake.positions
        assert {(x.ai_review or {}).get("decision") for x in db.scalars(select(TradingSignal))
                if x.decision in ("BUY", "SELL")} <= {"UNREVIEWED", None}


def test_engine_only_keeps_every_risk_control(scan, monkeypatch):
    """Paused entries still block engine-only execution: the mode changes who confirms, never the Risk Engine."""
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    set_mode("ENGINE_ONLY")
    with session_scope() as db:
        tr.get_state(db).pause_new_entries = True
    scan_once(bridge, None, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        assert not db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED")).all() and not fake.positions
