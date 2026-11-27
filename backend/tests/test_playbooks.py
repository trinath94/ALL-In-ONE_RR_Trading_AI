"""Market playbooks: the AI's trader plan per symbol and regime, saved and applied by the engine to every candidate,
reused when proven, graded from its trades. It can only restrict; it never forces a trade."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai import gateway
from app.db.models import ClosedPosition, Symbol, TradeOrder, TradingSignal
from app.db.models_ai import AIControl, MarketPlaybook
from app.db.session import session_scope
from app.intelligence import playbook as pbk
from app.services import trading as tr
from app.workers.main import _playbook_size
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_engine_first import NoAI, run_scan
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)

PLAN = {"bias": "BUY", "allowed_directions": ["BUY"], "preferred_strategies": ["trend"], "avoid": ["Asian range"],
        "risk_posture": "normal", "size_multiplier": 1.0, "reasoning": "H4 and D1 trend up; buy pullbacks."}


def with_ai(monkeypatch, plans):
    pid = provider("claude")
    route("market_playbook", (pid, "m"))
    fake = Scripted({"claude": list(plans)})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    return fake


def test_the_ai_plan_is_saved_and_reused_for_every_candidate(scan, monkeypatch, keys):
    fake = with_ai(monkeypatch, [PLAN] * 10)
    reviews, orders, _ = run_scan(scan, monkeypatch)
    shortlisted = [r for r in reviews if r]
    assert orders, "candidates that fit the plan go through the Risk Engine to the broker"
    assert all(r["playbook"]["allow"] for r in shortlisted if r.get("mode") == "ENGINE")
    with session_scope() as db:
        pbs = db.scalars(select(MarketPlaybook)).all()
        assert pbs and all(p.source == "ai" and p.status == "ACTIVE" for p in pbs), [(p.symbol, p.regime, p.status) for p in pbs]
        assert len(fake.seen) == len({p.symbol for p in pbs}), "one AI call per symbol, not per candidate or timeframe"
        assert "timeframes" in pbs[0].facts and "recent_results" in pbs[0].facts, "engine facts, never raw data"
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == pbs[0].symbol))
        calls = len(fake.seen)
        assert len({(p.symbol, p.regime) for p in pbs}) == len(pbs), "one plan per symbol: timeframes share it"
        again, how = pbk.get_or_create(db, sym, pbk.plan_regime(db, sym, None))
        assert how == "active" and again.id == pbs[0].id and len(fake.seen) == calls


def test_stand_aside_blocks_and_never_forces(scan, monkeypatch, keys):
    with_ai(monkeypatch, [{**PLAN, "risk_posture": "stand_aside", "bias": "NEUTRAL"}] * 10)
    reviews, orders, fake = run_scan(scan, monkeypatch)
    blocked = [r for r in reviews if r.get("mode") == "PLAYBOOK"]
    assert blocked and not orders and not fake.positions
    st = type("S", (), {"auto_trading": True, "auto_trading_since": None, "ai_mode": "AI"})()
    sig = type("G", (), {"decision": "BUY", "created_at": None, "ai_review": blocked[0]})()
    assert "stand aside" in tr.auto_execution_block(st, sig)


def test_direction_against_the_plan_is_skipped_and_reduced_posture_halves_size():
    pb = MarketPlaybook(id=7, symbol="EURUSD.r", regime="TREND_UP", bias="SELL", allowed_directions=["SELL"],
                        risk_posture="normal", size_multiplier=1.0, source="ai")
    assert pbk.apply(pb, "BUY")["allow"] is False and "against the plan" in pbk.apply(pb, "BUY")["reason"]
    pb.allowed_directions, pb.risk_posture, pb.size_multiplier = ["BUY", "SELL"], "reduced", 0.8
    v = pbk.apply(pb, "BUY")
    assert v["allow"] and v["size_multiplier"] == 0.5
    sig = type("G", (), {"ai_review": {"size_multiplier": 0.5}})()
    assert _playbook_size(sig) == 0.5
    for bad in (2.0, 0, -1, "x", None):
        assert _playbook_size(type("G", (), {"ai_review": {"size_multiplier": bad}})()) == 1.0, "never above normal"


def symbol(db):
    return db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))


def test_a_proven_plan_is_reused_without_an_ai_call_and_a_failed_one_is_not(stack, keys, monkeypatch):
    def no_ai(hop, factory):
        raise AssertionError("no AI call expected")
    monkeypatch.setattr(gateway, "_provider", no_ai)
    now = datetime.now(UTC)
    with session_scope() as db:
        for net, trades in ((-5.0, 6), (12.5, 4)):
            db.add(MarketPlaybook(symbol="EURUSD.r", regime="TREND_UP", bias="BUY", allowed_directions=["BUY"],
                                  preferred_strategies=[], avoid=[], risk_posture="normal", size_multiplier=1.0,
                                  reasoning="r", facts={}, source="ai", status="EXPIRED",
                                  outcomes={"trades": trades, "net": net}, valid_from=now - timedelta(days=2),
                                  valid_until=now - timedelta(days=1)))
    with session_scope() as db:
        pb, how = pbk.get_or_create(db, symbol(db), "TREND_UP")
        good = db.scalar(select(MarketPlaybook).where(MarketPlaybook.outcomes["net"].astext == "12.5"))
        assert how == "reused" and pb.source == "reused" and pb.reused_from == good.id and pb.tokens == 0
        pb2, how2 = pbk.get_or_create(db, symbol(db), "RANGE")
        assert pb2 is None and how2 == "none:no AI provider", "no proven plan for this regime and no provider"
        assert db.get(MarketPlaybook, pb.id).status == "SUPERSEDED", "a regime change retires the old plan"


def test_daily_cap_and_switch(stack, keys, monkeypatch):
    with_ai(monkeypatch, [PLAN])
    with session_scope() as db:
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           playbook_daily_cap=0))
    with session_scope() as db:
        assert pbk.get_or_create(db, symbol(db), "TREND_UP") == (None, "none:daily cap 0 reached")
        db.get(AIControl, 1).playbook_enabled = False
    with session_scope() as db:
        assert pbk.get_or_create(db, symbol(db), "TREND_UP") == (None, "none:playbooks off")


def test_playbooks_are_graded_from_the_trades_they_governed(stack):
    now = datetime.now(UTC)
    with session_scope() as db:
        pb = MarketPlaybook(symbol="EURUSD.r", regime="TREND_UP", bias="BUY", allowed_directions=["BUY"],
                            preferred_strategies=[], avoid=[], risk_posture="normal", size_multiplier=1.0, reasoning="r",
                            facts={}, source="ai", status="ACTIVE", outcomes={}, valid_from=now,
                            valid_until=now - timedelta(minutes=1))
        db.add(pb)
        db.flush()
        sym, acct = symbol(db), tr.active_account(db)
        for i, profit in enumerate((10.0, -4.0)):
            sig = TradingSignal(symbol_id=sym.id, decision="BUY", grade="A", timeframe="H1", strategies=[],
                                expires_at=now + timedelta(hours=1),
                                ai_review={"decision": "SKIPPED", "mode": "ENGINE", "playbook": {"id": pb.id}})
            db.add(sig)
            db.flush()
            db.add(TradeOrder(execution_id=f"e{i}", account_id=acct.id, signal_id=sig.id, symbol="EURUSD.r",
                              action="MARKET_BUY", volume=0.1, source="AUTO", status="CONFIRMED"))
            db.add(ClosedPosition(account_id=acct.id, ticket=100 + i, symbol="EURUSD.r", direction="BUY", volume=0.1,
                                  open_price=1.1, close_price=1.1, profit=profit, execution_id=f"e{i}", mode="DEMO",
                                  opened_at=now, closed_at=now))
        pid = pb.id
    with session_scope() as db:
        out = pbk.grade(db)
        p = db.get(MarketPlaybook, pid)
        assert out == {"graded": 1, "expired": 1} and p.status == "EXPIRED"
        assert (p.outcomes["trades"], p.outcomes["wins"], p.outcomes["net"]) == (2, 1, 6.0)


def test_playbooks_api(stack):
    from tests.test_api import h, login  # noqa: F401
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_api import make_user
    make_user("admin", "admin")
    c = TestClient(app)
    login(c, "admin")
    r = c.get("/api/ai/playbooks").json()
    assert r["playbooks"] == [] and r["today"] == {"ai": 0, "reused": 0}
    s = c.put("/api/ai/engine-first", json={"playbook_hours": 6, "playbook_daily_cap": 4}, headers=h(c)).json()
    assert s["playbook_hours"] == 6 and s["playbook_daily_cap"] == 4 and s["playbook_enabled"] is True
    assert c.put("/api/ai/engine-first", json={"playbook_hours": 0}, headers=h(c)).status_code == 422
