"""Historical trade intelligence (RR Trading One Phase 10, specification Part 10), built on the existing Part 25/26
tables: exit types from the broker's deal reasons, recorded-only attribution, entry/exit quality, costs, durations,
coverage, DEMO/LIVE separation, sources kept apart, AI trade research through the gateway, measured peer correlation."""
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import BrokerConnection, ClosedPosition, MT5Terminal, StrategyPerformance, Strategy, TradeExecution, \
    TradeOrder, TradingAccount
from app.db.models_ai import AgentRun
from app.db.models_intel import AIRecommendation, MT5Deal, TradeFeatures
from app.db.session import session_scope
from app.learning_engine import performance as perf
from app.scanner.service import peer_correlation
from tests.test_ai_gateway import Scripted, provider, route
from tests.test_api import client, h, login  # noqa: F401 (fixture)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def accounts():
    with session_scope() as db:
        term = MT5Terminal(name="t", bridge_url="http://x")
        db.add(term)
        db.flush()
        bc = BrokerConnection(broker_name="B", server="S", terminal_id=term.id)
        db.add(bc)
        db.flush()
        demo = TradingAccount(broker_connection_id=bc.id, login=1, server="S", mode="DEMO")
        live = TradingAccount(broker_connection_id=bc.id, login=2, server="S", mode="LIVE")
        db.add_all([demo, live])
        db.flush()
        return demo.id, live.id


def trade(acct, mode, ticket, *, net, r=None, minutes=30, codes=(), execution=None, reason=None, close_reason="CLOSED",
          commission=-0.7, swap=0.0, mae=None, mfe=None, slippage=None, outcome=None, partial=1):
    with session_scope() as db:
        cp = ClosedPosition(account_id=acct, ticket=ticket, symbol="EURUSD.r", direction="BUY", volume=0.1,
                            open_price=1.1, close_price=1.101, stop_loss=1.099, profit=net - commission - swap,
                            commission=commission, swap=swap, r_multiple=r, opened_at=T0,
                            closed_at=T0 + timedelta(minutes=minutes), close_reason=close_reason, mode=mode,
                            execution_id=execution, strategy_codes=list(codes), partial_exits=partial,
                            sync_status="COMPLETE")
        db.add(cp)
        db.flush()
        if reason is not None:
            db.add(MT5Deal(account_id=acct, mode=mode, deal_ticket=ticket * 10, position_id=ticket, symbol="EURUSD.r",
                           deal_type=1, entry=1, reason=reason, volume=0.1, price=1.101, deal_time=cp.closed_at))
        db.add(TradeFeatures(closed_position_id=cp.id, mode=mode, symbol="EURUSD.r", outcome=outcome or (
            "WIN" if net > 0 else "LOSS"), mae_r=mae, mfe_r=mfe, slippage_points=slippage, partial_exits=partial or 0,
            entry_method=codes[0] if codes else ("MANUAL" if execution else "EXTERNAL")))
        return cp.id


@pytest.fixture
def book(db_clean):
    demo, live = accounts()
    trade(demo, "DEMO", 1, net=20, r=2.0, codes=["smc_bos"], execution="e1", reason=5, mae=0.25, mfe=2.5, slippage=1.0)
    trade(demo, "DEMO", 2, net=-10, r=-1.0, codes=["smc_bos"], execution="e2", reason=4, mae=1.0, mfe=0.4)
    trade(demo, "DEMO", 3, net=0.3, r=0.03, execution="e3", reason=3, minutes=600, mae=0.5, mfe=0.05, outcome="BE",
          swap=-1.2, partial=3)
    trade(demo, "DEMO", 4, net=-5, r=-0.5, reason=0, minutes=2000)                 # closed by hand in the terminal
    trade(demo, "DEMO", 5, net=7, r=None, close_reason="STOP_LOSS")                  # no ledger deal: collector reason
    trade(demo, "DEMO", 6, net=3, r=None, close_reason="CLOSED", partial=None)       # nothing recorded: UNKNOWN
    trade(live, "LIVE", 7, net=-50, r=-1.0, codes=["smc_bos"], execution="e7", reason=4)
    return demo, live


def by_ticket(trades):
    with session_scope() as db:
        ids = {c.id: c.ticket for c in db.scalars(select(ClosedPosition))}
    return {ids[t["id"]]: t for t in trades}


def test_exit_types_come_from_the_broker_deal_reason(book):
    with session_scope() as db:
        t = by_ticket(perf.load_trades(db, "DEMO"))
    assert {k: v["exit_type"] for k, v in t.items()} == {
        1: "TAKE_PROFIT", 2: "STOP_LOSS", 3: "PROGRAM", 4: "MANUAL", 5: "STOP_LOSS", 6: "UNKNOWN"}


def test_attribution_is_recorded_never_guessed(book):
    with session_scope() as db:
        trades = perf.load_trades(db, "DEMO")
    t = by_ticket(trades)
    assert [t[k]["attribution"] for k in (1, 2, 3, 4)] == ["ENGINE_SIGNAL", "ENGINE_SIGNAL", "ENGINE_MANUAL", "UNLINKED"]
    strat = perf.by_dimension(trades)["strategy"]
    assert set(strat) == {"smc_bos"} and strat["smc_bos"]["sample_size"] == 2, "unlinked trades get no strategy"


def test_quality_costs_durations_and_coverage(book):
    with session_scope() as db:
        trades = perf.load_trades(db, "DEMO")
    t = by_ticket(trades)
    assert t[1]["entry_quality"] == 0.75 and t[1]["exit_efficiency"] == 0.8          # 2.0 R kept of 2.5 R available
    assert t[2]["entry_quality"] == 0.0 and t[3]["exit_efficiency"] is None           # MFE < 0.1 R: undefined
    assert t[2]["exit_efficiency"] == -1.0, "-1 R after only +0.4 R in favour: clipped, cannot dominate the average"
    assert [t[k]["duration"] for k in (1, 3, 4)] == ["15m-1h", "8h-24h", ">1d"]
    m = perf.compute_metrics(trades)
    assert m["sample_size"] == 6 and m["outcomes"] == {"WIN": 3, "LOSS": 2, "BE": 1}
    assert m["exit_types"] == {"TAKE_PROFIT": 1, "STOP_LOSS": 2, "PROGRAM": 1, "MANUAL": 1, "UNKNOWN": 1}
    assert m["total_commission"] == -4.2 and m["total_swap"] == -1.2
    assert m["avg_slippage_points"] == {"value": 1.0, "sample": 1}, "missing measurements are left out, not filled in"
    assert m["avg_entry_quality"]["sample"] == 3 and m["avg_exit_efficiency"] == {"value": -0.1, "sample": 2}
    assert m["coverage"]["realized_r"] == 4 and m["coverage"]["mae_mfe"] == 3 and m["coverage"]["spread"] == 0
    assert m["trades_with_partial_exits"] == 1 and m["partial_exits_known"] == 5
    assert m["holding_seconds"]["sample"] == 6
    dims = perf.by_dimension(trades)
    assert {"exit_type", "attribution", "duration"} <= set(dims) and dims["exit_type"]["MANUAL"]["sample_size"] == 1


def test_demo_and_live_never_mix_and_sources_stay_apart(client, book):
    with session_scope() as db:
        assert [t["mode"] for t in perf.load_trades(db, "LIVE")] == ["LIVE"]
        assert all(t["mode"] == "DEMO" for t in perf.load_trades(db, "DEMO"))
        sid = db.scalar(select(Strategy.id).where(Strategy.code == "smc_bos"))
        db.add_all([StrategyPerformance(strategy_id=sid, source="OOS", trades=40, wins=22, gross_profit=300, gross_loss=200),
                    StrategyPerformance(strategy_id=sid, source="BACKTEST", trades=500, wins=300, gross_profit=9000,
                                        gross_loss=3000)])
    login(client, "viewer")
    s = client.get("/api/learning/sources").json()
    assert s["closed_trades"]["DEMO"]["sample_size"] == 6 and s["closed_trades"]["LIVE"]["sample_size"] == 1
    assert s["closed_trades"]["LIVE"]["net_profit"] == -50.0
    assert s["strategy_performance"]["OOS"]["trades"] == 40 and s["strategy_performance"]["BACKTEST"]["profit_factor"] == 3.0
    assert "No figure here combines them" in s["note"]
    p = client.get("/api/learning/performance", params={"mode": "DEMO", "dimension": "exit_type"}).json()
    assert set(p["dimensions"]["exit_type"]) == {"TAKE_PROFIT", "STOP_LOSS", "PROGRAM", "MANUAL", "UNKNOWN"}


@pytest.fixture
def ai(monkeypatch):
    monkeypatch.setenv("RR_AI_KEYS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RR_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_trade_research_through_the_gateway(client, book, ai, monkeypatch):
    from app.ai import gateway
    login(client, "admin")
    assert client.post("/api/learning/trade-research", params={"mode": "DEMO"}, headers=h(client)).status_code == 409
    pid = provider("qwen")
    route("trade_research", (pid, "qwen-max"))
    answer = {"findings": ["Stop-loss exits dominate losses (n=2, inconclusive)"], "weaknesses": [],
              "hypotheses_to_test": ["Later break-even"], "data_gaps": ["No spread recorded"]}
    fake = Scripted({"qwen": [answer, {**answer, "lot_size": 2}]})
    monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
    r = client.post("/api/learning/trade-research", params={"mode": "DEMO"}, headers=h(client))
    assert r.status_code == 200 and r.json()["findings"] == answer["findings"]
    sent = json.dumps(fake.seen[0]["user"])
    assert '"mode": "DEMO"' in sent and "ticket" not in sent and "login" not in sent and "-50" not in sent, \
        "aggregates of one mode only; no tickets, accounts or LIVE figures"
    with session_scope() as db:
        rec = db.scalar(select(AIRecommendation))
        assert (rec.source, rec.status, rec.evidence["trades"]) == ("trade_research", "OPEN", 6)
        run = db.scalar(select(AgentRun).where(AgentRun.agent_id == "post_trade_learning"))
        assert run.provider == "qwen" and run.output["recommendation_id"] == rec.id
    r = client.post("/api/learning/trade-research", params={"mode": "DEMO"}, headers=h(client))
    assert r.status_code == 502 and "invalid output" in r.text, "an answer with fields outside the schema is refused"
    login(client, "trader")
    assert client.post("/api/learning/trade-research", params={"mode": "DEMO"}, headers=h(client)).status_code == 403


def test_peer_correlation_is_measured_with_its_sample():
    idx = pd.date_range("2026-01-01", periods=250, freq="h", tz="UTC")
    rng = np.random.default_rng(3)
    a = pd.DataFrame({"close": 1.1 * np.exp(np.cumsum(rng.normal(0, 0.001, 250)))}, index=idx)
    same = a.copy()
    inverse = pd.DataFrame({"close": 1 / a["close"]}, index=idx)
    short = a.iloc[-20:]
    out = peer_correlation(a, {"SAME": same, "INV": inverse, "SHORT": short})
    assert out["SAME"] == {"rho": 1.0, "bars": 200} and out["INV"]["rho"] == -1.0
    assert out["SHORT"] == {"rho": None, "bars": 19, "note": "fewer than 30 common bars"}, "below 30 bars: no figure"
    flat = pd.DataFrame({"close": [1.1] * 250}, index=idx)
    assert peer_correlation(flat, {"A": a})["A"]["note"].startswith("undefined"), "zero variance is not a correlation"
