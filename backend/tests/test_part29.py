"""Parts 27-30 Phase 6 (Part 29): strategy selection decisions, model/provider health, systematic parameter variants,
the bias report, strategy DNA, optional exits (time, volatility, structure, invalidation), strategy concentration,
and the LIVE readiness checklist that gates the engine's LIVE switch. Simulated broker only; LIVE is never enabled
outside the in-process test of the switch's refusal and of the existing LIVE flow."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (AuditLog, ClosedPosition, OpenPosition, RiskConfig, StrategyExperiment, TradingAccount,
                           User)
from app.db.models_opportunity import Opportunity
from app.db.session import session_scope
from app.intelligence import selection
from app.risk import engine as risk
from app.scanner.service import scan_once
from app.services import readiness
from app.services import trading as tr
from app.strategies import registry
from app.strategies.experimental import variants
from app.workers import main as worker
from tests.test_api import client, h, login, make_user  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)


def seed_demo_trades(n: int, profit: float = 5.0) -> None:
    """Closed DEMO trades on record (the LIVE readiness checklist's DEMO acceptance)."""
    now = datetime.now(UTC)
    with session_scope() as db:
        acct = db.scalar(select(TradingAccount).where(TradingAccount.mode == "DEMO"))
        start = 900000 + (db.query(ClosedPosition).count())
        for i in range(n):
            db.add(ClosedPosition(account_id=acct.id, ticket=start + i, symbol="EURUSD.r", direction="BUY", volume=0.1,
                                  open_price=1.1, close_price=1.101, profit=profit, opened_at=now - timedelta(hours=2),
                                  closed_at=now - timedelta(hours=1), close_reason="TP", mode="DEMO"))


# ---- Risk Engine: concentration and optional exits ------------------------------------------------------------------
def view(**kw):
    base = dict(ticket=1, symbol="EURUSD", direction="BUY", volume=0.1, open_price=1.1, current_price=1.1005,
                stop_loss=1.098, take_profit=1.106)
    return risk.PositionView(**{**base, **kw})


def test_new_risk_parameters_are_off_by_default_and_validated():
    p = risk.RiskParams()
    assert (p.max_positions_per_strategy, p.max_holding_minutes, p.volatility_exit_atr_ratio, p.structure_exit,
            p.invalidation_exit) == (0, 0, 0.0, False, False)
    now = datetime.now(UTC)
    assert risk.exit_actions(view(), p, opened_at=now - timedelta(days=9), now=now, atr_now=9.0, atr_entry=1.0,
                             choch_against=True, engine_view="SELL") == [], "all off: no exit, whatever happens"
    bad = risk.RiskParams.from_dict({"max_holding_minutes": -1, "volatility_exit_atr_ratio": 0.5})
    assert any("max_holding_minutes" in e for e in bad.validate()) and any("volatility_exit" in e for e in bad.validate())


@pytest.mark.parametrize("params,kw,expect", [
    ({"max_holding_minutes": 60}, {"opened_at_ago": 61}, "time exit"),
    ({"max_holding_minutes": 60}, {"opened_at_ago": 59}, None),
    ({"volatility_exit_atr_ratio": 2.0}, {"atr_now": 2.1, "atr_entry": 1.0}, "volatility exit"),
    ({"volatility_exit_atr_ratio": 2.0}, {"atr_now": 1.9, "atr_entry": 1.0}, None),
    ({"volatility_exit_atr_ratio": 2.0}, {"atr_now": 5.0, "atr_entry": None}, None),        # unknown: no exit
    ({"structure_exit": True}, {"choch_against": True}, "structure exit"),
    ({"invalidation_exit": True}, {"engine_view": "SELL"}, "invalidation exit"),
    ({"invalidation_exit": True}, {"engine_view": "NO_TRADE"}, "invalidation exit"),
    ({"invalidation_exit": True}, {"engine_view": "WAIT"}, None),
    ({"invalidation_exit": True}, {"engine_view": None}, None),                              # unknown: no exit
])
def test_each_optional_exit(params, kw, expect):
    now = datetime.now(UTC)
    acts = risk.exit_actions(view(), risk.RiskParams.from_dict(params),
                             opened_at=now - timedelta(minutes=kw.get("opened_at_ago", 1)), now=now,
                             atr_now=kw.get("atr_now"), atr_entry=kw.get("atr_entry"),
                             choch_against=kw.get("choch_against", False), engine_view=kw.get("engine_view"))
    if expect is None:
        assert acts == []
    else:
        assert len(acts) == 1 and acts[0]["action"] == "CLOSE" and acts[0]["reason"].startswith(expect)


def test_concentration_blocks_a_second_order_from_the_same_strategy_end_to_end(stack):
    fake, bridge = stack
    allow_unknown_news()
    with session_scope() as db:
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "DEMO"))
        rc.params = {**rc.params, "max_positions_per_strategy": 1, "max_trades_per_symbol": 5, "max_open_positions": 5}
    worker.startup_checks(bridge)
    with session_scope() as db:
        o = open_eur(db, bridge, key="c1", strategy_codes=["smc_bos"])
        assert o.status == "CONFIRMED"
    with session_scope() as db:
        tr.sync_positions(db, bridge, tr.active_account(db))
    with session_scope() as db:
        from app.execution import manager as em
        with pytest.raises(em.ExecutionBlocked, match="strategy_concentration|strategies already at 1"):
            em.open_position(db, bridge, symbol="XAUUSD.r", direction="BUY", stop_loss=1990.0, take_profit=2030.0,
                             source="MANUAL", dedupe_key="c2", strategy_codes=["smc_bos"])


def test_time_exit_closes_through_the_journalled_manager_only_when_enabled(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="t1")
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        tr.sync_positions(db, bridge, tr.active_account(db))
        db.scalar(select(OpenPosition).where(OpenPosition.ticket == ticket)).opened_at = datetime.now(UTC) - timedelta(hours=3)
    worker.manage_positions(bridge)
    assert ticket in fake.positions, "exits are off by default"
    with session_scope() as db:
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "DEMO"))
        rc.params = {**rc.params, "max_holding_minutes": 120}
    worker.manage_positions(bridge)
    assert ticket not in fake.positions, "held 3 h > 2 h: closed"
    from app.execution.manager import TradeOrder
    with session_scope() as db:
        assert db.scalar(select(TradeOrder).where(TradeOrder.action == "CLOSE", TradeOrder.position_ticket == ticket))


# ---- selection and health --------------------------------------------------------------------------------------------
def res(decision="BUY", direction="BUY", strategies=None):
    return SimpleNamespace(decision=decision, direction=direction,
                           strategies=strategies or [{"strategy": "smc_bos", "direction": "BUY"},
                                                     {"strategy": "trend_macd", "direction": "BUY"}])


def test_selection_decisions():
    metas = registry.all_strategies()
    good = {"smc_bos": {"trades": 40, "wins": 24, "gp": 400, "gl": 200}}
    bad = {"smc_bos": {"trades": 40, "wins": 10, "gp": 100, "gl": 300}, "trend_macd": {"trades": 50, "wins": 15, "gp": 50, "gl": 90}}
    assert selection.assess(res("NO_TRADE", None), {}, metas, "SIDEWAYS", [])["decision"] == "NO_TRADE"
    assert selection.assess(res("WAIT"), {}, metas, "SIDEWAYS", [])["decision"] == "WAIT"
    r = selection.assess(res(), bad, metas, "SIDEWAYS", [])
    assert r["decision"] == "REJECT" and r["evidence"] == "proven"
    one = selection.assess(res(strategies=[{"strategy": "smc_bos", "direction": "BUY"}]), good, metas, "STRONG_BULL", [])
    assert one["decision"] == "SELECT" and one["lead"] == "smc_bos" and one["strategies"][0]["profit_factor"] == 2.0
    thin = selection.assess(res(), {}, metas, "STRONG_BULL", [])
    assert thin["evidence"] == "unproven" and thin["decision"] in ("SELECT", "COMBINATION")
    regs = {c: metas[c].regimes for c in ("smc_bos", "trend_macd")}
    label = next(iter(set(regs["smc_bos"] or ["STRONG_BULL"]) & set(regs["trend_macd"] or ["STRONG_BULL"])), "STRONG_BULL")
    combo = selection.assess(res(), good, metas, label, [])
    assert combo["decision"] == "COMBINATION", combo


def test_health_reports_drifting_production_models(db_clean):
    from app.db.models_intel import DriftEvent, ModelRegistry
    with session_scope() as db:
        db.add(ModelRegistry(model_key="prob_tp_before_sl", version=3, algorithm="x", target="y", status="PRODUCTION"))
        db.add(DriftEvent(scope="feature", subject="prob_tp_before_sl", metric="psi", value=0.4, threshold=0.2,
                          severity="warning"))
    with session_scope() as db:
        hl = selection.health(db)
    assert hl["models_healthy"] is False and hl["models"][0]["detail"].startswith("drift event")


def test_scan_trace_has_selection_and_health(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        o = db.scalars(select(Opportunity)).first()
        stages = {t["stage"]: t for t in o.trace}
        assert stages["strategy_selection"]["result"] in ("SELECT", "COMBINATION", "WAIT", "REJECT", "NO_TRADE")
        assert stages["health"]["result"] in ("healthy", "degraded")
        assert o.score_breakdown["selection"]["decision"] == stages["strategy_selection"]["result"]


# ---- variants, bias report, DNA --------------------------------------------------------------------------------------
def bars(n=900, seed=3):
    rng = np.random.default_rng(seed)
    c = 1.1 + np.cumsum(rng.normal(0, 0.0012, n))
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.0006, "low": np.minimum(o, c) - 0.0006, "close": c,
                         "volume": 100.0, "spread": 10.0}, index=idx)


CODE = "trend_ema_9_21"


def test_variant_sets_change_one_numeric_parameter_at_a_time():
    vs = variants.variant_sets(CODE)
    base = dict(registry.get(CODE).params)
    assert vs and all(len(v["changed"]) == 1 for v in vs)
    for v in vs:
        (k, (old, new)), = v["changed"].items()
        assert base[k] == old and v["params"][k] == new and {**v["params"], k: old} == base


def test_generate_records_experiments_without_touching_the_strategy(db_clean):
    before = dict(registry.get(CODE).params)
    with session_scope() as db:
        out = variants.generate(db, CODE)
    assert out["created"] and not out["already_existed"]
    with session_scope() as db:
        again = variants.generate(db, CODE)
        rows = db.scalars(select(StrategyExperiment)).all()
    assert not again["created"] and len(again["already_existed"]) == len(out["created"]), "no duplicates"
    assert all(r.stage == "DISCOVERED" and r.source_code is None and r.proposal["kind"] == "parameter_variant" for r in rows)
    assert dict(registry.get(CODE).params) == before and len(registry.all_strategies()) == 215


def test_variant_evaluation_and_bias_report(db_clean):
    with session_scope() as db:
        variants.generate(db, CODE)
    df = bars()
    spec = {"symbol": "EURUSD", "timeframe": "H1", "point": 0.00001, "tick_size": 0.00001, "tick_value": 1.0,
            "spread_points": 10}
    with session_scope() as db:
        e = db.scalars(select(StrategyExperiment)).first()
        out = variants.evaluate(db, e, df, spec)
        eid = e.id
    b = out["bias"]
    assert b["look_ahead"]["passed"] and b["leakage"]["passed"] and b["passed"], b
    assert b["multiple_testing"]["variants_tried"] >= 2 and b["multiple_testing"]["required_p"] < 0.05
    assert set(out["base"]) == {"in_sample", "out_of_sample"} and "profit_factor" in out["variant"]["out_of_sample"]
    with session_scope() as db:
        assert "variant_evaluation" in db.get(StrategyExperiment, eid).evidence


def test_bias_report_flags_repainting_and_leakage(monkeypatch):
    """A backtest whose earlier trades change when later bars exist is flagged (simulated by a backtester that
    shifts every trade when given more data); so is an out-of-sample trade entered before the cut."""
    from app.backtesting import engine as bt
    df = bars(600)
    real = bt.run_backtest

    def repainting(d, cfg, start=None, end=None):
        out = real(d, cfg, start, end)
        if len(d) == len(df):
            for t in out["trades"]:
                t["exit"] = t["exit"] + 0.001
        return out
    monkeypatch.setattr(bt, "run_backtest", repainting)
    rep = variants.bias_report(None, df, CODE, dict(registry.get(CODE).params), {"spread_points": 10})
    if rep["look_ahead"]["compared_trades"]:
        assert rep["look_ahead"]["passed"] is False and rep["passed"] is False
    monkeypatch.setattr(bt, "run_backtest", real)

    def leaky_split(d, cfg, oos_fraction=0.3):
        r = {"in_sample": {"trades": []}, "out_of_sample": {"trades": [{"entry_time": str(d.index[10])}]},
             "cut_time": str(d.index[int(len(d) * 0.7)])}
        return r
    monkeypatch.setattr(bt, "split_oos", leaky_split)
    rep2 = variants.bias_report(None, df, CODE, dict(registry.get(CODE).params), {"spread_points": 10})
    assert rep2["leakage"]["passed"] is False and rep2["passed"] is False


def test_variants_and_dna_api(client):
    make_user("viewer29", "readonly")
    login(client, "viewer29")
    assert client.post(f"/api/strategies/{CODE}/variants", headers=h(client)).status_code == 403
    dna = client.get(f"/api/strategies/{CODE}/dna").json()
    assert dna["code"] == CODE and dna["variants"] == [] and "params" in dna and dna["family"]
    login(client, "admin")
    r = client.post(f"/api/strategies/{CODE}/variants", headers=h(client))
    assert r.status_code == 200 and r.json()["created"]
    assert client.post("/api/strategies/nope/variants", headers=h(client)).status_code == 404
    dna = client.get(f"/api/strategies/{CODE}/dna").json()
    assert len(dna["variants"]) == len(r.json()["created"]) == dna["variants_tried"]
    assert client.get("/api/strategies/nope/dna").status_code == 404
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "STRATEGY_VARIANTS"))


# ---- LIVE readiness -------------------------------------------------------------------------------------------------
def approved_live_account():
    with session_scope() as db:
        demo = db.scalar(select(TradingAccount).where(TradingAccount.mode == "DEMO"))
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.add(TradingAccount(broker_connection_id=demo.broker_connection_id, login=2002, server="Live-Server",
                              mode="LIVE", is_active=True, verified=True, live_approved_by=admin_id))


def test_readiness_requires_a_demo_track_record_and_gates_the_switch(client, stack):
    login(client, "admin")
    approved_live_account()
    rep = client.get("/api/live/readiness").json()
    items = {i["key"]: i for i in rep["items"]}
    assert rep["ready"] is False and rep["failing"] == ["demo_acceptance"]
    assert items["identity"]["passed"] and items["live_risk_config"]["passed"] and items["executions_resolved"]["passed"]
    assert items["pc_connector_lock"]["required"] is False and items["pc_connector_lock"]["passed"] is None
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 409 and "readiness" in r.text and "closed DEMO trades" in r.text
    seed_demo_trades(20)
    assert client.get("/api/live/readiness").json()["ready"] is True
    with session_scope() as db:
        assert tr.get_state(db).active_mode == "DEMO", "the checklist never switches anything itself"


def test_readiness_refuses_a_live_risk_config_looser_than_demo(db_clean):
    with session_scope() as db:
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "LIVE"))
        demo = tr.risk_params(db, "DEMO")
        rc.params = {**rc.params, "risk_per_trade_pct": demo.risk_per_trade_pct + 0.5}
    with session_scope() as db:
        item = next(i for i in readiness.report(db)["items"] if i["key"] == "live_risk_config")
    assert item["passed"] is False and "risk_per_trade_pct" in item["detail"]


def test_readiness_endpoint_needs_trade_control(client):
    make_user("viewer29b", "readonly")
    login(client, "viewer29b")
    assert client.get("/api/live/readiness").status_code == 403
