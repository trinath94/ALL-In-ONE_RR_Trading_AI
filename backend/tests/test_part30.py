"""Parts 27-30 Phase 7 (Part 30): pending-order reconciliation and emergency cancellation, Risk Governor additions
(floating loss, strategy allocation, volatility sizing), market data quality, dataset versions, model and strategy
governance, monitoring (API latency/errors, error logs, DB connections, job backlog), scenario simulation and
counterfactual alternatives. Simulated broker only."""
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.backtesting import scenarios as sc
from app.db.models import (ClosedPosition, RiskConfig, Strategy, StrategyPerformance, SystemEvent, TradeOrder,
                           TradingAccount)
from app.db.models_intel import DriftEvent, LearningJob, ModelRegistry
from app.db.session import session_scope
from app.execution import manager as em
from app.execution import reconcile
from app.intelligence import data_quality, selection
from app.learning_engine import governance
from app.learning_engine.versioning import dataset_version
from app.risk import engine as risk
from app.scanner.service import scan_once
from app.services import monitoring
from app.services import trading as tr
from app.services.events import get_redis
from app.workers import main as worker
from tests.test_api import h, login, make_user
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)


# ---- reconciliation ---------------------------------------------------------------------------------------------------
def test_engine_and_connector_agree_on_the_order_tag():
    from connector import service as cs
    assert reconcile.MAGIC == cs.MAGIC
    assert all(reconcile.tag(e) == cs.tag(e) for e in ("abc", "0f" * 16, "x"))


def place_limit(bridge, key="L1"):
    with session_scope() as db:
        return em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.0900, take_profit=1.1100,
                                source="MANUAL", dedupe_key=key, order_type="LIMIT", price=1.0950)


def test_startup_compares_pending_orders_and_blocks_on_unknown_engine_orders(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    o = place_limit(bridge)
    assert o.status == "CONFIRMED" and len(fake.pending) == 1
    fake.pending[90001] = dict(ticket=90001, symbol="XAUUSD.r", type=2, volume_current=0.1, price_open=1990.0, sl=0, tp=0,
                               magic=0, comment="manual", time_setup=0)
    ok, report = worker.startup_checks(bridge)
    line = next(x for x in report if x.startswith("pending orders compared"))
    assert ok and "1 matched" in line and "1 foreign" in line and "0 unknown" in line, report
    fake.pending[90002] = dict(ticket=90002, symbol="EURUSD.r", type=2, volume_current=0.1, price_open=1.09, sl=0, tp=0,
                               magic=reconcile.MAGIC, comment=reconcile.tag("not-in-the-journal"), time_setup=0)
    with session_scope() as db:
        tr.get_state(db).startup_checks_passed = False
    ok, report = worker.startup_checks(bridge)
    assert not ok and "engine-tagged pending orders the journal does not know" in report[-1], report
    with session_scope() as db:
        assert db.scalar(select(SystemEvent).where(SystemEvent.message == "Unknown engine pending orders at the broker"))


def test_kill_switch_cancels_only_the_engines_pending_orders(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    place_limit(bridge, "L2")
    fake.pending[90001] = dict(ticket=90001, symbol="XAUUSD.r", type=2, volume_current=0.1, price_open=1990.0, sl=0, tp=0,
                               magic=0, comment="manual", time_setup=0)
    with session_scope() as db:
        acts = em.kill_switch(db, bridge, None, "test emergency")
    cancels = [a for a in acts if a.get("action") == "CANCEL_PENDING"]
    assert len(cancels) == 1 and cancels[0]["status"] == "CONFIRMED", acts
    assert list(fake.pending) == [90001], "foreign orders are never touched"
    with session_scope() as db:
        assert db.scalar(select(TradeOrder).where(TradeOrder.action == "CANCEL_PENDING", TradeOrder.status == "CONFIRMED"))
        acts2 = em.kill_switch(db, bridge, None, "again")
    assert not [a for a in acts2 if a.get("action") == "CANCEL_PENDING"], "nothing left to cancel"


# ---- Risk Governor ----------------------------------------------------------------------------------------------------
SPEC = risk.SymbolSpec("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100, 0.01)


def acct(balance=10000.0, equity=10000.0):
    return risk.AccountState(1, "s", "DEMO", "USD", balance, equity, 0, equity, 100, equity, equity, equity, equity)


def req(**kw):
    return risk.OrderRequest("EURUSD", "BUY", 1.1, 1.098, 1.106, 5, margin_required_per_lot=100.0, **kw)


def test_new_governor_checks_are_off_by_default():
    d = risk.evaluate(req(volatility_percentile=0.95, strategy_codes=["a"]), SPEC, acct(equity=9000),
                      [risk.OpenRisk("X", "BUY", 1, 500, 1000, strategy_codes=("a",))], risk.RiskParams(), risk.ControlState())
    names = {c.name for c in d.checks}
    assert not names & {"floating_loss", "strategy_allocation", "volatility_sizing"}


def test_floating_loss_limit_blocks_new_entries():
    p = risk.RiskParams.from_dict({"max_floating_loss_pct": 2.0, "max_daily_loss_pct": 50, "max_weekly_loss_pct": 50,
                                   "max_monthly_loss_pct": 50, "max_drawdown_pct": 50})
    d = risk.evaluate(req(), SPEC, acct(balance=10000, equity=9700), [], p, risk.ControlState())
    c = next(c for c in d.checks if c.name == "floating_loss")
    assert not c.passed and not d.approved and "3.00% under water" in c.detail
    ok = risk.evaluate(req(), SPEC, acct(balance=10000, equity=9900), [], p, risk.ControlState())
    assert next(c for c in ok.checks if c.name == "floating_loss").passed


def test_strategy_allocation_and_volatility_sizing():
    p = risk.RiskParams.from_dict({"max_strategy_risk_pct": 1.5, "risk_per_trade_pct": 1.0})
    pos = [risk.OpenRisk("XAUUSD", "BUY", 0.1, 100.0, 1000.0, strategy_codes=("smc_bos",))]
    d = risk.evaluate(req(strategy_codes=["smc_bos"]), SPEC, acct(), pos, p, risk.ControlState())
    c = next(c for c in d.checks if c.name == "strategy_allocation")
    assert not c.passed and not d.approved, c
    other = risk.evaluate(req(strategy_codes=["trend_macd"]), SPEC, acct(), pos, p, risk.ControlState(news_status="CLEAR"))
    assert next(c for c in other.checks if c.name == "strategy_allocation").passed
    vs = risk.RiskParams.from_dict({"volatility_sizing": True})
    clear = risk.ControlState(news_status="CLEAR")                    # the existing news policy is not the subject here
    calm = risk.evaluate(req(volatility_percentile=0.4), SPEC, acct(), [], vs, clear)
    wild = risk.evaluate(req(volatility_percentile=0.9), SPEC, acct(), [], vs, clear)
    assert calm.approved and wild.approved and wild.volume < calm.volume, (calm.volume, wild.volume)
    assert "risk budget x0.40" in next(c for c in wild.checks if c.name == "volatility_sizing").detail
    assert any("max_floating_loss_pct" in e for e in risk.RiskParams.from_dict({"max_floating_loss_pct": -1}).validate())


# ---- data quality -----------------------------------------------------------------------------------------------------
def rows(n=50, start=datetime(2026, 11, 10, tzinfo=UTC)):           # a Tuesday
    t0 = int(start.timestamp())
    return [{"time": t0 + i * 3600, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1} for i in range(n)]


def test_data_quality_checks():
    now = datetime.fromtimestamp(rows()[-1]["time"] + 7200, UTC)
    clean = data_quality.assess(rows(), 60, now=now, tick={"time": now.timestamp() - 2}, source="mt5:EURUSD:H1")
    assert clean["score"] == 100 and clean["problems"] == {} and clean["source"] == "mt5:EURUSD:H1"
    r = rows()
    r.insert(10, dict(r[10]))                                          # duplicate
    r[20], r[21] = r[21], r[20]                                        # out of order
    r[30] = {**r[30], "high": 1.0}                                     # high below low
    del r[40:43]                                                       # a weekday gap
    bad = data_quality.assess(r, 60, now=now, tick={"time": now.timestamp() - 600})
    assert set(bad["problems"]) == {"duplicate", "out_of_order", "invalid_price", "gap", "stale_quote"}, bad
    assert bad["score"] < 60 and bad["gaps"][0]["missing_bars"] == 3 and bad["quote_age_s"] == 600
    weekend = rows(24, datetime(2026, 11, 13, 0, tzinfo=UTC)) + rows(24, datetime(2026, 11, 16, 0, tzinfo=UTC))
    assert "gap" not in data_quality.assess(weekend, 60, now=datetime(2026, 11, 17, 1, tzinfo=UTC))["problems"]


def test_scan_records_data_quality(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    from app.db.models import AnalysisResult
    with session_scope() as db:
        a = db.scalars(select(AnalysisResult)).first()
        dq = a.structure["data_quality"]
        assert 0 <= dq["score"] <= 100 and dq["source"].startswith("mt5:") and "problems" in dq


# ---- dataset versions and governance ---------------------------------------------------------------------------------
def test_dataset_versions_are_deterministic_and_content_sensitive(db_clean):
    a = pd.DataFrame({"x": [1, 2, 3], "y": [0, 1, 0]})
    assert dataset_version(a) == dataset_version(a.copy()) and dataset_version(a).startswith("sha256:")
    b = a.copy()
    b.loc[1, "x"] = 9
    assert dataset_version(a) != dataset_version(b) and dataset_version(np.array([1.0])) != dataset_version(np.array([2.0]))
    from app.learning_engine import adaptive
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")                          # no trades: rejected, but versioned
        assert reg.dataset_ref["dataset_version"] == dataset_version([])


def test_validation_expiry_drift_and_strategy_suspension(db_clean):
    old = datetime.now(UTC) - timedelta(days=200)
    with session_scope() as db:
        db.add(ModelRegistry(model_key="prob_tp_before_sl", version=1, algorithm="x", target="y", status="PRODUCTION",
                             validated_at=old, dataset_ref={"dataset_version": "sha256:abc"}))
        db.add(ModelRegistry(model_key="consensus_weights", version=2, algorithm="x", target="y", status="PRODUCTION",
                             validated_at=datetime.now(UTC)))
        s = db.scalar(select(Strategy).where(Strategy.code == "smc_bos"))
        db.add(StrategyPerformance(strategy_id=s.id, source="PAPER", trades=40, wins=12, gross_profit=60.0, gross_loss=100.0))
        s2 = db.scalar(select(Strategy).where(Strategy.code == "trend_macd"))
        db.add(StrategyPerformance(strategy_id=s2.id, source="PAPER", trades=10, wins=1, gross_profit=1.0, gross_loss=100.0))
    with session_scope() as db:
        ms = {m["model"]: m for m in governance.models(db)}
        hl = selection.health(db)
        sus = governance.strategy_suspensions(db)
    assert ms["prob_tp_before_sl"]["validation_expired"] and not ms["prob_tp_before_sl"]["healthy"]
    assert ms["prob_tp_before_sl"]["dataset_version"] == "sha256:abc"
    assert ms["consensus_weights"]["healthy"] and hl["models_healthy"] is False
    assert [x["strategy"] for x in sus] == ["smc_bos"], "fewer than 30 trades is never a suspension"
    with session_scope() as db:
        assert db.scalar(select(Strategy.enabled).where(Strategy.code == "smc_bos")) is True, "nothing is disabled"


# ---- monitoring -------------------------------------------------------------------------------------------------------
def test_monitoring_snapshot_counts_requests_errors_and_logs(db_clean):
    import logging
    monitoring.record_request(10.0, 200)
    monitoring.record_request(30.0, 503)
    h = monitoring.ErrorCounter("testrole")
    h.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "boom", None, None))
    with session_scope() as db:
        snap = monitoring.snapshot(db)
    assert snap["api"]["requests"] == 2 and snap["api"]["server_errors"] == 1 and snap["api"]["mean_latency_ms"] == 20.0
    assert snap["log_errors_15m"]["testrole"] == 1
    assert snap["db_connections"]["total"] >= 1 and snap["db_connections"]["max"] > 0


def test_backlog_alerts_once_for_overdue_and_stuck_jobs(db_clean):
    now = datetime.now(UTC)
    with session_scope() as db:
        db.add(LearningJob(job_type="trade_sync", idempotency_key="a", status="DONE", finished_at=now - timedelta(hours=2)))
        db.add(LearningJob(job_type="portfolio", idempotency_key="b", status="RUNNING", started_at=now - timedelta(hours=3)))
    with session_scope() as db:
        msgs = monitoring.check_backlog(db)
    assert any("trade_sync overdue" in m for m in msgs) and any("portfolio running since" in m for m in msgs)
    with session_scope() as db:
        monitoring.check_backlog(db)
        assert len(db.scalars(select(SystemEvent).where(SystemEvent.message == "Job backlog")).all()) == 1, "once an hour"


# ---- scenarios and counterfactuals -------------------------------------------------------------------------------------
def path(points, legs=10):
    xs = [points[0]]
    for a, b in zip(points, points[1:]):
        xs += list(np.linspace(a, b, legs + 1)[1:])
    c = np.array(xs)
    idx = pd.date_range("2026-01-01", periods=len(c), freq="h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.0002, "low": np.minimum(o, c) - 0.0002, "close": c},
                        index=idx)


S = sc.Setup("BUY", 1.1000, 1.0950, 1.1100)


def test_walk_outcomes():
    assert sc.walk(path([1.1, 1.12]), S)["outcome"] == "TARGET" and sc.walk(path([1.1, 1.12]), S)["r"] == 2.0
    assert sc.walk(path([1.1, 1.09]), S)["outcome"] == "STOP" and sc.walk(path([1.1, 1.09]), S)["r"] == -1.0
    gap = path([1.1, 1.101])
    gap.iloc[0, gap.columns.get_loc("open")] = 1.0900
    g = sc.walk(gap, S)
    assert g["outcome"] == "STOP_GAP" and g["r"] < -1.0
    t = sc.walk(path([1.1, 1.104]), sc.Setup("BUY", 1.1, 1.095, 1.11, max_bars=5))
    assert t["outcome"] == "TIME" and t["bars"] == 5
    assert sc.walk(path([1.1, 1.12]), sc.Setup("BUY", 1.1, 1.105, 1.11))["outcome"] == "INVALID"


def test_scenarios_stress_one_setup():
    out = {x["scenario"]: x for x in sc.scenarios(path([1.1, 1.104, 1.098, 1.112]), S)}
    assert set(out) == {"base", "continuation", "reversal", "volatility_x2", "spread_x3", "slippage_shock", "gap_through_stop"}
    assert out["reversal"]["outcome"] == "STOP" and out["gap_through_stop"]["outcome"] == "STOP_GAP"
    assert out["continuation"]["outcome"] == "TARGET"
    s2 = sc.Setup("BUY", 1.1, 1.095, 1.11, spread=0.0005)
    assert sc.scenarios(path([1.1, 1.12]), s2)[4]["r"] < sc.scenarios(path([1.1, 1.12]), s2)[0]["r"], "spread x3 costs more"


def test_counterfactuals_are_labelled_hindsight():
    cf = sc.counterfactuals(path([1.1, 1.097, 1.12]), S)
    kinds = {x["kind"] for x in cf["alternatives"]}
    assert kinds == {"stop_target", "exit_timing", "limit_entry"} and len(cf["alternatives"]) == 16 + 4 + 2
    assert cf["best_in_hindsight"]["r"] >= cf["actual"]["r"] and "hindsight" in cf["note"]
    filled = [x for x in cf["alternatives"] if x["kind"] == "limit_entry" and x["outcome"] != "NOT_FILLED"]
    assert filled, "1.0975 was traded at, so the 0.5 R better limit fills"


def test_simulation_api_from_an_opportunity_and_a_closed_position(scan, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    scan_once(bridge, None, lambda _id: None)
    shift[0] = 12                                                      # later bars now exist after the entry
    from app.main import app
    from app.db.models_opportunity import Opportunity
    with session_scope() as db:
        oid = db.scalars(select(Opportunity).where(Opportunity.signal_id.isnot(None))).first().id
        a = db.scalar(select(TradingAccount).where(TradingAccount.mode == "DEMO"))
        db.add(TradeOrder(execution_id="cf-1", account_id=a.id, symbol="EURUSD.r", action="MARKET_BUY", volume=0.1,
                          stop_loss=1.0900, take_profit=1.1100, status="CONFIRMED"))
        cp = ClosedPosition(account_id=a.id, ticket=777001, symbol="EURUSD.r", direction="BUY", volume=0.1,
                            open_price=1.1, close_price=1.1, stop_loss=1.0900, profit=0.0,
                            opened_at=datetime.now(UTC) - timedelta(hours=20), closed_at=datetime.now(UTC) - timedelta(hours=10),
                            execution_id="cf-1", mode="DEMO")
        db.add(cp)
        db.flush()
        cid = cp.id
    from app.api.routes.trading import bridge_dep
    app.dependency_overrides[bridge_dep] = lambda: bridge           # the simulated broker of this test
    make_user("viewer30", "readonly")
    try:
        _simulation_api(app, oid, cid)
    finally:
        app.dependency_overrides.pop(bridge_dep, None)


def _simulation_api(app, oid, cid):
    with TestClient(app) as c:
        login(c, "viewer30")
        assert c.post("/api/simulations", json={"kind": "scenario", "opportunity_id": oid}, headers=h(c)).status_code == 403
        assert c.get("/api/simulations").json() == []
        make_user("admin30", "admin")
        login(c, "admin30")
        r = c.post("/api/simulations", json={"kind": "scenario", "opportunity_id": oid}, headers=h(c))
        assert r.status_code == 200, r.text
        assert len(r.json()["result"]["rows"]) == 7 and r.json()["bars"] > 0
        r2 = c.post("/api/simulations", json={"kind": "counterfactual", "closed_position_id": cid}, headers=h(c))
        assert r2.status_code == 200, r2.text
        assert "best_in_hindsight" in r2.json()["result"]
        assert c.post("/api/simulations", json={"kind": "scenario"}, headers=h(c)).status_code == 422
        assert len(c.get("/api/simulations").json()) == 2
        mon = c.get("/api/system/monitoring").json()
        assert mon["api"]["requests"] >= 1 and "backlog" in mon
        assert c.get("/api/governance").status_code == 200
        dq = c.get("/api/data-quality").json()
        assert dq and all("score" in d for d in dq)
