"""Adaptive strategy intelligence (RR Trading One Phase 12, specification Part 12): profiles proposed only from
validated realised evidence, rejected on noise, signed, shadow before production, administrator promotion, the original
engine weights untouched as the baseline; ranking, slices, deterioration, combinations, redundancy, regimes,
failures and parameter stability built on the existing modules."""
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import AnalysisResult, BacktestJob, ClosedPosition, OptimisationResult, Symbol, TradeOrder, \
    TradingAccount, TradingSignal
from app.db.models_intel import FailureReport, ModelPrediction, ModelRegistry, RegimeHistory, TradeFeatures
from app.db.session import session_scope
from app.learning_engine import adaptive, consensus_weights
from app.learning_engine import models as mreg
from app.scanner import service as scanner
from app.signals.engine import MarketContext, SignalConfig, build_signal
from app.strategies.base import C_LIQUIDITY, C_SMC_ICT, StrategySignal
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_model_governance import approver
from tests.test_trade_intelligence import accounts

T0 = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
# +-1 R per trade in the favourable / unfavourable regime, against noise of 1 R: validated in 40 of 40 probes with 400
# trades, while pure noise validated in 0 of 200.
EFFECT = {("A", "TRENDING"): 1.0, ("A", "RANGING"): -1.0, ("B", "TRENDING"): -1.0, ("B", "RANGING"): 1.0}


def mk_symbol(db, acct: int) -> Symbol:
    s = Symbol(broker_connection_id=db.get(TradingAccount, acct).broker_connection_id, broker_symbol="EURUSD.r",
               canonical="EURUSD", category="forex", point=0.00001, tick_size=0.00001, tick_value=1, contract_size=100000,
               volume_min=0.01, volume_max=100, volume_step=0.01)
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def signing(db_clean, monkeypatch):
    monkeypatch.setenv("RR_MODEL_SIGNING_KEY", "s" * 48)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def book(acct: int, mode: str, n: int, *, signal: bool, seed: int, start: int = 0) -> None:
    """``n`` realised trades, one every 6 hours. With ``signal`` strategy A earns in TRENDING and loses in RANGING, B
    the reverse, C and D nothing; without it every R is noise."""
    rng = np.random.default_rng(seed)
    with session_scope() as db:
        for i in range(n):
            regime = "TRENDING" if rng.random() < 0.5 else "RANGING"
            codes = sorted(set(rng.choice(["A", "B", "C", "D"], size=rng.integers(1, 3), replace=False)))
            r = float(rng.normal(0, 1))
            if signal:
                r += float(np.mean([EFFECT.get((c, regime), 0.0) for c in codes]))
            t = T0 + timedelta(hours=6 * (start + i))
            cp = ClosedPosition(account_id=acct, ticket=10_000 + start + i, symbol="EURUSD.r", direction="BUY",
                                volume=0.1, open_price=1.1, close_price=1.1, stop_loss=1.09, profit=r * 10, commission=0,
                                swap=0, r_multiple=r, opened_at=t - timedelta(minutes=30), closed_at=t, mode=mode,
                                strategy_codes=codes, regime=regime, timeframe="H1", sync_status="COMPLETE")
            db.add(cp)
            db.flush()
            db.add(TradeFeatures(closed_position_id=cp.id, mode=mode, symbol="EURUSD.r", timeframe="H1", regime=regime,
                                 session="LONDON" if i % 2 else "NEW_YORK", outcome="WIN" if r > 0 else "LOSS"))


# ------------------------------------------------------------------ the baseline is untouched ---------------------
def ctx(**kw) -> MarketContext:
    base = dict(symbol="EURUSD", timeframe="H1", price=1.1, bar_time=T0, atr=0.001, spread=0.00001, regime="TRENDING",
                regime_tags=[], htf_bias=1, sessions=["LONDON"], news_status="CLEAR")
    return MarketContext(**{**base, **kw})


SIGS = [StrategySignal("A", "BUY", 1.1, 1.098, 1.106, 0.8, ["a"], C_SMC_ICT),
        StrategySignal("B", "BUY", 1.1, 1.098, 1.106, 0.7, ["b"], C_LIQUIDITY)]


def test_engine_without_a_profile_is_exactly_the_original():
    a = build_signal(SIGS, ctx(), SignalConfig()).dict()
    b = build_signal(SIGS, ctx(adaptive={}), SignalConfig()).dict()
    c = build_signal(SIGS, ctx(adaptive={"A": 1.0, "B": 1.0}), SignalConfig()).dict()
    assert a == b == c, "an empty or neutral profile changes nothing"
    down = build_signal(SIGS, ctx(adaptive={"A": 0.5}), SignalConfig())
    assert down.score < a["score"] and down.components[C_SMC_ICT] == pytest.approx(0.4)


# ------------------------------------------------------------------ proposals --------------------------------------
def test_too_little_evidence_is_rejected_and_the_baseline_stays(signing):
    demo, _ = accounts()
    book(demo, "DEMO", 60, signal=True, seed=1)
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        assert reg.status == "REJECTED" and "INSUFFICIENT DATA: 60 DEMO trades" in reg.notes and reg.artifact is None
        assert adaptive.active_profiles(db) == {"production": None, "shadow": None}


def test_real_regime_edge_validates_a_signed_profile_from_one_mode_only(signing):
    demo, live = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    book(live, "LIVE", 400, signal=False, seed=3, start=1000)   # LIVE noise must not leak into a DEMO profile
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        m = reg.metrics
        assert reg.status == "VALIDATED", reg.notes
        assert reg.dataset_ref["mode"] == "DEMO" and reg.dataset_ref["trades"] == 400
        assert m["holdout"]["improvement_ci95"][0] > 0 and m["walk_forward"]["passed"]
        for f in m["walk_forward"]["folds"]:
            assert f["train_end"] < f["test_start"], "every fold is fitted on the past only"
        assert mreg.signature_state(reg) == "valid"
        mults = json.loads(reg.artifact)["multipliers"]
        assert mults["A"]["TRENDING"] > 1 > mults["A"]["RANGING"] and mults["B"]["RANGING"] > 1 > mults["B"]["TRENDING"]
        assert all(adaptive.MULT_MIN <= v <= adaptive.MULT_MAX for c in mults.values() for v in c.values())
        prof = adaptive.active_profiles(db)
        assert prof["production"] is None and prof["shadow"][0].id == reg.id, "validated means shadow, not applied"


@pytest.mark.parametrize("seed", [4, 5, 6, 7, 8])
def test_noise_never_validates(signing, seed):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=False, seed=seed)
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        assert reg.status == "REJECTED", reg.metrics


def test_a_tampered_profile_is_never_used(signing):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        assert adaptive.active_profiles(db)["shadow"] is not None
        evil = json.loads(reg.artifact)
        evil["multipliers"]["C"] = {"*": 1.5}
        blob = json.dumps(evil).encode()
        reg.artifact, reg.artifact_sha256 = blob, hashlib.sha256(blob).hexdigest()   # integrity recomputed, as an attacker would
        db.flush()
        assert mreg.signature_state(reg) == "invalid" and adaptive.active_profiles(db)["shadow"] is None


# ------------------------------------------------------------------ shadow, promotion, rollback --------------------
def test_shadow_is_recorded_beside_the_unchanged_decision_then_promotion_applies_it(signing):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    sigs = [StrategySignal("A", "BUY", 1.1, 1.098, 1.106, 0.95, ["a"], C_SMC_ICT),
            StrategySignal("B", "BUY", 1.1, 1.098, 1.106, 0.95, ["b"], C_LIQUIDITY)]
    mc = ctx(regime="RANGING")
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        baseline = build_signal(sigs, mc, SignalConfig())
        used, recs = scanner._adaptive(db, sigs, mc, SignalConfig(), None, baseline)
        assert used is baseline, "shadow never changes the decision"
        f = recs[0].features
        assert recs[0].shadow and recs[0].model_id == reg.id and f["baseline_score"] == baseline.score
        assert f["adaptive_score"] != baseline.score and f["multipliers"]["A"] < 1 < f["multipliers"]["B"]
        uid = approver(db)
        with pytest.raises(PermissionError):
            mreg.transition(db, reg, "APPROVED", None)                     # never without an authorised user
        mreg.transition(db, reg, "APPROVED", uid)
        mreg.transition(db, reg, "PRODUCTION", uid)
        used, recs = scanner._adaptive(db, sigs, mc, SignalConfig(), None, baseline)
        assert used is not baseline and used.score == f["adaptive_score"] and not recs[0].shadow
        # back to the original weights: retire the profile
        mreg.transition(db, reg, "RETIRED", uid)
        used, recs = scanner._adaptive(db, sigs, mc, SignalConfig(), None, baseline)
        assert used is baseline and recs == []


def test_shadow_evaluation_counts_only_what_is_known(signing):
    demo, _ = accounts()
    with session_scope() as db:
        reg = ModelRegistry(model_key=adaptive.MODEL_KEY, version=1, algorithm="x", target="r", status="VALIDATED")
        db.add(reg)
        sym = mk_symbol(db, demo)
        for i, (base, alt, r) in enumerate([("BUY", "NO_TRADE", -1.0), ("BUY", "WAIT", -1.0), ("BUY", "BUY", 2.0),
                                            ("NO_TRADE", "SELL", None)]):
            sig = TradingSignal(symbol_id=sym.id, decision=base, grade="A", timeframe="H1", score=70, strategies=[],
                                regime="RANGING", explanation="", expires_at=T0)
            db.add(sig)
            db.flush()
            db.add(ModelPrediction(model_id=reg.id, signal_id=sig.id, symbol="EURUSD.r", timeframe="H1", raw_score=1,
                                   features={"baseline_decision": base, "adaptive_decision": alt}))
            if r is not None:
                db.add(TradeOrder(signal_id=sig.id, execution_id=f"e{i}", account_id=demo, symbol="EURUSD.r",
                                  action="MARKET_BUY", volume=0.1, status="CONFIRMED"))
                db.add(ClosedPosition(account_id=demo, ticket=i, symbol="EURUSD.r", direction="BUY", volume=0.1,
                                      open_price=1.1, close_price=1.1, profit=r, r_multiple=r, opened_at=T0,
                                      closed_at=T0, execution_id=f"e{i}", mode="DEMO"))
        db.flush()
        ev = adaptive.shadow_evaluation(db, reg)
    assert ev["decisions"] == 4 and ev["agreement"] == 0.25
    assert ev["baseline_traded_profile_would_not"] == 2 and ev["profile_would_trade_baseline_did_not"] == 1
    assert ev["skipped_trades_with_outcome"] == 2 and ev["r_avoided"] == 2.0 and ev["verdict"] == "INSUFFICIENT_DATA"


# ------------------------------------------------------------------ analysis --------------------------------------
def test_ranking_slices_combinations_and_deterioration(signing):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    with session_scope() as db:
        trades = adaptive.realised(db, "DEMO")
        rank = adaptive.ranking(trades, {"E": {"OOS": {"trades": 12, "verdict": "INSUFFICIENT_DATA"}}})
        assert {r["strategy"] for r in rank} == {"A", "B", "C", "D", "E"}
        e = next(r for r in rank if r["strategy"] == "E")
        assert e["rank"] is None and e["realised"]["sample_size"] == 0, "backtest evidence alone never ranks"
        assert all(r["rank"] is None or r["realised"]["sample_size"] >= 30 for r in rank)
        sl = adaptive.best_by_slice(trades)
        assert [c["strategy"] for c in sl["regime"]["TRENDING"]["best"]][:1] == ["A"]
        assert [c["strategy"] for c in sl["regime"]["RANGING"]["best"]][:1] == ["B"]
        combos = adaptive.combination_value(trades)
        assert combos and all(c["together"] >= 30 for c in combos)
        assert adaptive.deterioration(db, trades) == [], "a stationary edge has not deteriorated"
    # a strategy that stops working: its last 30 trades lose
    with session_scope() as db:
        for cp in db.scalars(select(ClosedPosition).where(ClosedPosition.strategy_codes.contains(["A"]))
                             .order_by(ClosedPosition.closed_at.desc()).limit(30)):
            cp.r_multiple = -1.5
        trades = adaptive.realised(db, "DEMO")
        det = adaptive.deterioration(db, trades)
        assert [d["strategy"] for d in det] == ["A"] and det[0]["change_ci95"][1] < 0


def test_co_firing_redundancy_and_conflict(db_clean):
    demo, _ = accounts()
    with session_scope() as db:
        sym = mk_symbol(db, demo)
        for i in range(40):
            outs = [{"strategy": "A", "direction": "BUY"}, {"strategy": "A2", "direction": "BUY"},
                    {"strategy": "X", "direction": "SELL" if i % 4 else "BUY"}]
            db.add(AnalysisResult(symbol_id=sym.id, timeframe="H1", bar_time=T0 + timedelta(hours=i), regime="RANGING",
                                  structure={}, strategy_outputs=outs, score={}))
        db.flush()
        cf = adaptive.co_firing(db)
    assert cf["analyses"] == 40
    assert [(p["a"], p["b"]) for p in cf["redundant"]] == [("A", "A2")]
    assert {(p["a"], p["b"]) for p in cf["conflicting"]} == {("A", "X"), ("A2", "X")}


def test_regime_outlook_failures_and_parameter_stability(signing):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    with session_scope() as db:
        for i, reg in enumerate(["TRENDING"] * 6 + ["RANGING"] * 3 + ["TRENDING"]):
            db.add(RegimeHistory(symbol="EURUSD.r", timeframe="H1", bar_time=T0 + timedelta(hours=i), regime=reg,
                                 probabilities={"TRENDING": 0.8, "RANGING": 0.2}, confidence=0.8, stability=0.7,
                                 model_version="t"))
        cps = db.scalars(select(ClosedPosition).where(ClosedPosition.r_multiple < 0).limit(10)).all()
        for cp in cps:
            db.add(FailureReport(closed_position_id=cp.id, mode="DEMO", outcome="LOSS",
                                 causes=[{"cause": "LATE_ENTRY", "evidence": "x", "confidence": 0.6}]))
        job = BacktestJob(kind="OPTIMISE", status="DONE", params={"strategies": ["A"], "symbol": "EURUSD", "timeframe": "H1"})
        db.add(job)
        db.flush()
        for rank, (p, pf_is, pf_oos) in enumerate([({"n": 10, "k": 2}, 2.0, 1.6), ({"n": 12, "k": 2}, 1.9, 1.4),
                                                   ({"n": 10, "k": 3}, 1.8, 0.9), ({"n": 14, "k": 3}, 1.7, 1.2)], 1):
            db.add(OptimisationResult(job_id=job.id, params=p, rank=rank, in_sample={"profit_factor": pf_is},
                                      out_of_sample={"profit_factor": pf_oos, "total_trades": 40}))
        db.flush()
        out = adaptive.regime_outlook(db, adaptive.realised(db, "DEMO"), "EURUSD.r", "H1")
        assert out["available"] and out["current"] == "TRENDING" and out["bars"] == 10
        assert out["persistence"]["TRENDING"] == pytest.approx(5 / 6, abs=1e-3)
        assert out["favoured"][0]["strategy"] == "A" and out["favoured"][0]["coverage"] == 1.0
        assert adaptive.regime_outlook(db, [], "GBPUSD", "H1")["available"] is False
        fi = adaptive.failure_intelligence(db, "DEMO")
        assert fi["losses_analysed"] == 10 and fi["strategies"][0]["top_causes"][0]["cause"] == "LATE_ENTRY"
        ps = adaptive.parameter_stability(db)
        assert ps[0]["verdict"] == "STABLE" and ps[0]["neighbours"] == 2 and ps[0]["neighbours_profitable_oos"] == 1
        # the winner's other neighbour also fails out of sample: a parameter cliff
        n12 = db.scalar(select(OptimisationResult).where(OptimisationResult.rank == 2))
        n12.out_of_sample = {"profit_factor": 0.8, "total_trades": 40}
        db.flush()
        assert adaptive.parameter_stability(db)[0]["verdict"] == "FRAGILE"
        best = db.scalar(select(OptimisationResult).where(OptimisationResult.rank == 1))
        best.out_of_sample = {"profit_factor": 0.95, "total_trades": 40}
        db.flush()
        assert adaptive.parameter_stability(db)[0]["verdict"] == "OVERFIT"


# ------------------------------------------------------------------ consensus weights (Phase 11 regression) -------
def test_consensus_weights_are_signed_approvable_and_read_only_from_the_artefact(signing, monkeypatch):
    from app.intelligence import consensus
    with session_scope() as db:
        X = np.random.default_rng(0).normal(size=(400, len(consensus_weights.VOTERS)))
        y = (X[:, 0] + 0.3 * np.random.default_rng(1).normal(size=400) > 0).astype(int)
        monkeypatch.setattr(consensus_weights, "dataset", lambda _db: (X, y))
        reg = consensus_weights.fit(db)
        assert reg.status == "VALIDATED" and mreg.signature_state(reg) == "valid"
        uid = approver(db)
        mreg.transition(db, reg, "APPROVED", uid)        # failed before: weights had no artefact to verify
        mreg.transition(db, reg, "PRODUCTION", uid)
        w, ver = consensus.validated_weights(db)
        assert ver == f"consensus_weights:v{reg.version}" and w == reg.metrics["weights"]
        reg.metrics = {**reg.metrics, "weights": {"SMC": 1.0}}   # editing the displayed metrics changes nothing
        assert consensus.validated_weights(db)[0] == w
        reg.artifact_hmac = "0" * 64
        assert consensus.validated_weights(db) == (None, f"UNVALIDATED_PRIOR:cw_v{reg.version}_unusable")


# ------------------------------------------------------------------ API --------------------------------------------
def test_adaptive_api(client, signing):
    demo, _ = accounts()
    book(demo, "DEMO", 400, signal=True, seed=2)
    login(client, "admin")
    ov = client.get("/api/intelligence/adaptive/overview?mode=DEMO").json()
    assert ov["evidence"]["trades"] == 400 and ov["weighting"] == "original signal engine weights (baseline)"
    assert ov["profiles"] == [] and ov["ranking"][0]["rank"] == 1
    assert client.get("/api/intelligence/adaptive/overview?mode=PAPER").status_code == 400
    r = client.post("/api/intelligence/adaptive/fit", json={"mode": "DEMO"}, headers=h(client))
    assert r.status_code == 200 and r.json()["status"] == "VALIDATED" and r.json()["signature"] == "valid"
    ov = client.get("/api/intelligence/adaptive/overview?mode=DEMO").json()
    assert ov["shadow"] == "v1" and ov["profiles"][0]["role"].startswith("shadow")
    assert ov["profiles"][0]["shadow_evaluation"]["decisions"] == 0
    for path in ("slices", "redundancy", "parameters", "regimes?symbol=EURUSD.r"):
        assert client.get(f"/api/intelligence/adaptive/{path}").status_code == 200, path
    login(client, "viewer")
    assert client.post("/api/intelligence/adaptive/fit", json={"mode": "DEMO"}, headers=h(client)).status_code == 403
