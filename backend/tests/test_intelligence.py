"""Part 26 intelligence: regime, models & governance, probability, consensus, anomaly, portfolio,
fingerprints, drift, execution analytics, trade-management safety."""
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from app.db.models import OpenPosition
from app.db.models_intel import (AnomalyEvent, ApprovalRecord, ModelRegistry, PositionAdjustment)
from app.db.session import session_scope
from app.execution import manager as em
from app.intelligence import anomaly, consensus, execution_analytics, fingerprint, portfolio, probability
from app.intelligence import regime as ri
from app.learning_engine import datasets, drift, models
from app.risk import engine as risk
from app.strategies.base import StrategySignal
from app.workers import main as worker
from tests.synthetic import make_ohlc
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401

UTC = timezone.utc


# ---------------------------------------------------------------- regime ---------------------------
def test_hmm_orders_states_by_volatility_and_detects_high_vol():
    rng = np.random.default_rng(1)
    r = np.r_[rng.normal(0, 0.001, 600), rng.normal(0, 0.006, 300)]
    close = 1.1 * np.exp(np.cumsum(r))
    idx = pd.date_range("2025-01-01", periods=len(close), freq="1h", tz="UTC")
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "close": close, "volume": 100}, index=idx)
    df["high"], df["low"] = df[["open", "close"]].max(axis=1) * 1.0005, df[["open", "close"]].min(axis=1) * 0.9995
    X = ri.hmm_features(df)
    m = ri.fit_hmm(X)
    assert list(np.argsort(m.means[:, -1])) == [0, 1, 2]
    p = ri.hmm_state_probs(m, X)
    assert p[-50:, 2].mean() > 0.7 and p[100:400, 0].mean() + p[100:400, 1].mean() > 0.7
    assert ri.cusum_last_change(np.abs(np.diff(np.log(close)))) is not None


def test_regime_intel_outputs_complete_record():
    df = make_ohlc(800, seed=2, freq="1h")
    out = ri.analyse(df, "CLEAR", {"trend_ema_9_21": ["STRONG_BULL"], "pa_range": ["SIDEWAYS"]})
    assert out.regime in ri.REGIMES and abs(sum(out.probabilities.values()) - 1) < 1e-3
    assert 0 <= out.stability <= 1 and out.model_version and out.details["baseline"]
    assert set(out.suitable) | set(out.unsuitable) == {"trend_ema_9_21", "pa_range"}
    assert ri.analyse(df.iloc[:50]).regime == "UNCERTAIN"


# ---------------------------------------------------------------- models & governance ----------------
def planted(n=3000, signal=True, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(0, 1, (n, len(datasets.FEATURES))), columns=datasets.FEATURES)
    logit = 1.6 * X["rsi"] - 1.0 * X["ret_5"] if signal else np.zeros(n)
    y = (rng.uniform(0, 1, n) < 1 / (1 + np.exp(-logit))).astype(float)
    X["y_tp_before_sl"] = y
    X["bar_index"] = np.arange(n) * 2
    X["time"] = pd.date_range("2024-01-01", periods=n, freq="2h", tz="UTC")
    X["bars_to_exit"], X["timeout"] = 10, False
    X["symbol"], X["timeframe"], X["strategy"], X["component"] = "SYN", "H1", "s", "trend"
    return X


def test_training_validates_real_signal_and_rejects_noise(migrated_db):
    with session_scope() as db:
        good = models.train(db, planted(signal=True), horizon=10, dataset_ref={"src": "planted"})
        assert good.status == "VALIDATED", good.notes
        assert good.metrics["test"]["brier_skill_score"] > 0 and good.calibration["test_reliability"]["ece"] < 0.1
        assert {"logistic_regression", "random_forest", "hist_gradient_boosting"} <= set(good.metrics["candidates"])
        bad = models.train(db, planted(signal=False, seed=3), horizon=10)
        assert bad.status == "REJECTED"
        tiny = models.train(db, planted(n=200), horizon=10)
        assert tiny.status == "REJECTED" and "LOW DATA" in tiny.notes
        db.rollback()


def test_embargo_prevents_overlap():
    ds = planted(1000)
    sp = datasets.chronological_split(ds, horizon=50)
    assert sp["train"].bar_index.max() + 50 < sp["validation"].bar_index.min()
    assert sp["validation"].bar_index.max() + 50 < sp["test"].bar_index.min() + 50


def test_governance_requires_approval_and_supports_rollback(db_clean):
    with session_scope() as db:
        v1 = models.train(db, planted(seed=1), horizon=10)
        v2 = models.train(db, planted(seed=2), horizon=10)
        assert v1.status == v2.status == "VALIDATED"
        with pytest.raises(ValueError):
            models.transition(db, v1, "PRODUCTION", 1)            # cannot skip approval
        with pytest.raises(PermissionError):
            models.transition(db, v1, "APPROVED", None)           # approval needs an authorised user
        from app.db.models import User, Role
        role = db.scalar(select(Role).where(Role.name == "admin"))
        u = User(username="approver", password_hash="x" * 20, role_id=role.id)
        db.add(u)
        db.flush()
        for v in (v1, v2):
            models.transition(db, v, "APPROVED", u.id)
            models.transition(db, v, "PRODUCTION", u.id)
        assert v1.status == "RETIRED" and v2.status == "PRODUCTION" and v2.rollback_to == v1.id
        prev = models.rollback(db, "prob_tp_before_sl", u.id)
        assert prev.id == v1.id and prev.status == "PRODUCTION"
        assert db.scalar(select(func.count(ApprovalRecord.id))) >= 6


def test_probability_unavailable_without_model_and_shadow_when_unapproved(db_clean):
    df = make_ohlc(600, seed=4, freq="1h")
    sig = {"direction": "BUY", "entry": float(df.close.iloc[-1]), "stop_loss": float(df.close.iloc[-1]) - 0.002,
           "take_profit": float(df.close.iloc[-1]) + 0.004, "strength": 0.6, "component": "trend"}
    with session_scope() as db:
        out = probability.estimate(db, df, sig, 1, 10, 1e-5, "SYN", "H1")
        assert out["available"] is False
        models.train(db, planted(seed=5), horizon=10)
        out = probability.estimate(db, df, sig, 1, 10, 1e-5, "SYN", "H1")
        assert out["available"] and out["shadow"] is True and out["model_status"] == "VALIDATED"
        assert out["raw_score"] is not None and "expected_r_net" in out and out["cost_r"] == pytest.approx(0.05, abs=1e-3)
        v = consensus.ml_specialist(out, "BUY")
        assert v.decision == "ABSTAIN"                            # shadow models never vote


# ---------------------------------------------------------------- consensus -------------------------
def s(code, d="BUY", comp="smc_ict", strength=0.7):
    return StrategySignal(code, d, 1.1, 1.098, 1.106, strength, ["r"], comp)


CATS = {"smc_fvg": "smc", "ict_ote": "ict", "liq_pdl": "liquidity", "trend_ema_9_21": "trend", "pa_pin_bar": "price_action"}


def test_consensus_prior_mode_counts_correlated_specialists_once(migrated_db):
    with session_scope() as db:
        only_structure = consensus.run(db, [s("smc_fvg"), s("ict_ote"), s("liq_pdl", comp="liquidity")], CATS, None, None, [], "BUY")
        assert only_structure["decision"] == "WAIT" and only_structure["weights_version"] == "UNVALIDATED_PRIOR"
        two_families = consensus.run(db, [s("smc_fvg"), s("trend_ema_9_21", comp="trend")], CATS, None, None, [], "BUY")
        assert two_families["decision"] == "BUY"
        conflict = consensus.run(db, [s("smc_fvg"), s("trend_ema_9_21", comp="trend"), s("pa_pin_bar", "SELL", "price_action")],
                                 CATS, None, None, [], "BUY")
        assert conflict["decision"] == "WAIT" and conflict["conflicts"]
        gated = consensus.run(db, [s("smc_fvg"), s("trend_ema_9_21", comp="trend")], CATS, None, None, ["critical anomaly"], "BUY")
        assert gated["decision"] == "NO_TRADE"
        regime_veto = consensus.run(db, [s("smc_fvg"), s("trend_ema_9_21", comp="trend")], CATS,
                                    {"regime": "UNCERTAIN", "confidence": 0.5, "transition_warning": "cp"}, None, [], "BUY")
        assert regime_veto["decision"] == "WAIT"
        assert len(two_families["specialists"]) == 10
        db.rollback()


# ---------------------------------------------------------------- anomaly ---------------------------
def test_anomaly_detection_rules():
    df = make_ohlc(300, seed=6, freq="1h")
    df["spread"] = 10
    last = df.index[-1]
    now = datetime(2026, 3, 4, 12, tzinfo=UTC)
    tick = {"bid": float(df.close.iloc[-1]), "ask": float(df.close.iloc[-1]) + 80e-5, "time": now.timestamp() - 600}
    found = {f["kind"]: f for f in anomaly.detect(df, tick, "H1", 1e-5, now=now)}
    assert found["abnormal_spread"]["blocks_entries"] and found["stale_price"]["blocks_entries"]
    flash = df.copy()
    flash.loc[last, "high"] = flash.loc[last, "close"] * 1.03
    assert any(f["kind"] == "flash_move" for f in anomaly.detect(flash, None, "H1", 1e-5, now=now))
    assert all(x["status"] == "unavailable" for x in anomaly.unavailable_sources())


def test_anomaly_blocks_entries_but_keeps_positions_and_recovers(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="pre-anomaly")
    sl_before = next(iter(fake.positions.values()))["sl"]
    crit = [{"kind": "flash_move", "severity": "critical", "explanation": "test", "metrics": {}, "blocks_entries": True}]
    with session_scope() as db:
        assert anomaly.record(db, "XAUUSD.r", crit)["blocking"] == ["flash_move"]
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="anomaly"):
        em.open_position(db, bridge, symbol="XAUUSD.r", direction="BUY", stop_loss=2390, take_profit=2420,
                         source="MANUAL", dedupe_key="during-anomaly")
    assert len(fake.positions) == 1 and next(iter(fake.positions.values()))["sl"] == sl_before   # untouched
    with session_scope() as db:
        for _ in range(anomaly.RECOVERY_CHECKS):
            r = anomaly.record(db, "XAUUSD.r", [])
        assert r["resolved"] == 1 and anomaly.active_block(db, "XAUUSD.r") is None
        assert db.scalar(select(AnomalyEvent.resolution)).startswith("condition clear")


def test_risk_engine_anomaly_gate_is_additive():
    from tests.test_risk import EURUSD, P, acct, req
    ok = risk.evaluate(req(), EURUSD, acct(), [], P, risk.ControlState(news_status="CLEAR"))
    blocked = risk.evaluate(req(), EURUSD, acct(), [], P, risk.ControlState(news_status="CLEAR", anomaly_block="flash"))
    assert ok.approved and not blocked.approved and any("anomaly" in r for r in blocked.reasons)


# ---------------------------------------------------------------- trade-management safety -----------
def test_stop_cannot_be_widened_and_every_adjustment_is_logged(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="sl-guard")
    ticket = next(iter(fake.positions))
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="widens risk"):
        em.manage(db, bridge, action="MODIFY_SLTP", position_ticket=ticket, dedupe_key="widen", stop_loss=1.0900)
    with session_scope() as db:
        em.manage(db, bridge, action="MODIFY_SLTP", position_ticket=ticket, dedupe_key="tighten", stop_loss=1.0990)
    with session_scope() as db:
        adj = db.scalars(select(PositionAdjustment).order_by(PositionAdjustment.id)).all()
        assert [a.accepted for a in adj] == [False, True]
        assert adj[0].previous_value == pytest.approx(1.098) and adj[0].new_value == pytest.approx(1.09)
    assert fake.positions[ticket]["sl"] == pytest.approx(1.099)


def test_stale_quote_cannot_authorise_entry(stack, monkeypatch):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    real = fake.symbol_info_tick
    monkeypatch.setattr(fake, "symbol_info_tick", lambda n: real(n)._replace(time=int(time.time()) - 3600))
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="Stale quote"):
        open_eur(db, bridge, key="stale")
    assert fake.send_calls == 0


# ---------------------------------------------------------------- portfolio ------------------------
def test_portfolio_correlation_and_same_underlying_risk(stack):
    fake, bridge = stack
    a = make_ohlc(700, seed=8, freq="1h")["close"]
    corr = portfolio.correlation_matrix({"A": a, "B": a * 1.01, "C": make_ohlc(700, seed=99, freq="1h")["close"]})
    assert corr[("A", "B")] > 0.99 and abs(corr[("A", "C")]) < 0.3
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="pf-1")
        from app.services import trading as tr
        acct = tr.active_account(db)
        p = db.scalar(select(OpenPosition))
        db.add(OpenPosition(account_id=acct.id, ticket=999, symbol="XAUUSD.r", direction="BUY", volume=0.01,
                            open_price=2400, current_price=2400, stop_loss=2390, opened_at=p.opened_at))
        db.flush()
        out = portfolio.assess(db, acct, {("EURUSD.r", "XAUUSD.r"): 0.95})
        assert out["metrics"]["effective_independent_bets"] < 1.2 and out["metrics"]["correlated_clusters"]
        assert out["metrics"]["total_open_risk"] > 0
        db.rollback()


# ---------------------------------------------------------------- fingerprints / drift / execution ---
def test_fingerprints_report_without_touching_registry(db_clean):
    from app.strategies import registry
    before = {m.code: m.code_hash for m in registry.metas()}
    with session_scope() as db:
        assert fingerprint.refresh(db)["new_fingerprints"] == 215
        assert fingerprint.refresh(db)["new_fingerprints"] == 0
        rep = fingerprint.duplicate_report(db)
        pairs = {(p["a"], p["b"]) for p in rep["static"]}
        assert ("trend_ema_20_50", "trend_ema_9_21") in pairs        # same logic, different parameters
    assert {m.code: m.code_hash for m in registry.metas()} == before
    beh = fingerprint.behavioural_similarity(make_ohlc(700, seed=3), ["pa_bull_flag", "pa_flag", "trend_ema_9_21"], step=1)
    assert "note" in beh


def test_psi_and_strategy_deterioration():
    assert drift.psi([0.1] * 10, [0.1] * 10) == pytest.approx(0)
    assert drift.psi([0.1] * 10, [0.3, 0.3] + [0.05] * 8) > 0.25


def test_execution_analytics_counts_only_confirmed_fills(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="ex-1")
    fake.reject_retcode = 10004
    with session_scope() as db:
        o = em.open_position(db, bridge, symbol="XAUUSD.r", direction="BUY", stop_loss=2390, take_profit=2420,
                             source="MANUAL", dedupe_key="ex-requote")
        assert o.status == "REJECTED"
    with session_scope() as db:
        a = execution_analytics.analyse(db)
        assert a["overall"]["orders"] == 2 and a["overall"]["fills"] == 1 and a["overall"]["requotes"] == 1
        assert a["overall"]["rejections"] == 1 and "EURUSD.r" in a["by_symbol"]
_ = (ModelRegistry, timedelta)
