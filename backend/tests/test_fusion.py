"""Probability and signal fusion (RR Trading One Phase 13, specification Part 13): one explainable decision from all
the evidence, never more aggressive than the original engine (the benchmark), WAIT/NO_TRADE never executable, a raw
score never presented as a probability, LOW DATA CONFIDENCE / INSUFFICIENT DATA when evidence is thin."""
import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.db.models import AnalysisResult, ClosedPosition, TradeOrder, TradingAccount, TradingSignal
from app.db.session import session_scope
from app.intelligence import fusion
from app.scanner import service as scanner
from app.signals.engine import SignalConfig, build_signal
from tests.test_adaptive import SIGS, ctx, mk_symbol
from tests.test_api import client, login  # noqa: F401 (fixture)
from tests.test_trade_intelligence import accounts

ENGINE = build_signal(SIGS, ctx(), SignalConfig())          # two BUY setups; decision set per test via engine()
RANK = {"NO_TRADE": 0, "WAIT": 1, "BUY": 2, "SELL": 2}


def engine(decision: str, calibrated=None, sample=0):
    d = "BUY" if decision != "NO_TRADE" else None
    return replace(ENGINE, decision=decision, direction=d, calibrated_confidence=calibrated, confidence_sample=sample)


def prob(er, low=False, shadow=False):
    return {"available": True, "shadow": shadow, "model": "prob_tp_before_sl:v3", "tp_before_sl_probability": 0.55,
            "calibration_bin_samples": 12 if low else 240, "low_data_confidence": low, "expected_r_net": er,
            "uncertainty_95": [0.5, 0.6]}


def cons(decision):
    return {"decision": decision, "agreement": 0.6, "conflicts": [], "weights_version": "UNVALIDATED_PRIOR"}


def run(e, *, consensus=None, probability=None, news="CLEAR", blocking=(), regime="WEAK_BULL", **kw):
    intel = {"regime": {"regime": regime, "confidence": 0.7, "stability": 0.8}, "probability": probability,
             "anomalies": {"found": [], "blocking": list(blocking)}}
    if consensus:
        intel["consensus"] = cons(consensus)
    return fusion.fuse(e, intel, news=news, spread=0.00001, atr=0.001, htf_bias=1, max_spread_atr=0.25, **kw)


def test_never_more_aggressive_than_the_original_and_never_reversed():
    seen = set()
    for dec, c, p, news, block in itertools.product(["BUY", "WAIT", "NO_TRADE"], [None, "BUY", "SELL", "NO_TRADE", "WAIT"],
                                                   [None, prob(0.4), prob(-0.2), prob(-0.2, low=True), prob(-0.2, shadow=True)],
                                                   ["CLEAR", "UNKNOWN", "RESTRICTED"], [(), ("STALE_QUOTES",)]):
        f = run(engine(dec), consensus=c, probability=p, news=news, blocking=block)
        assert RANK[f.decision] <= RANK[dec], (dec, f.decision)
        assert f.direction in (None, "BUY"), "never reversed"
        assert f.executable == (f.decision in ("BUY", "SELL")), "WAIT / NO_TRADE are never executable"
        assert f.benchmark["decision"] == dec
        seen.add((dec, f.decision))
    assert {("BUY", "BUY"), ("BUY", "WAIT"), ("BUY", "NO_TRADE"), ("WAIT", "WAIT"), ("WAIT", "NO_TRADE")} <= seen


def test_blocks_and_downgrades_are_explained():
    f = run(engine("BUY"), news="RESTRICTED")
    assert f.decision == "NO_TRADE" and "blocked: news restriction window" in f.explanation
    assert next(x for x in f.factors if x.factor == "news").effect == "blocks"
    f = run(engine("BUY"), blocking=["PRICE_GAP"])
    assert f.decision == "NO_TRADE" and next(x for x in f.factors if x.factor == "anomalies").status == "BLOCKS"
    f = run(engine("BUY"), consensus="SELL")
    assert f.decision == "WAIT" and f.direction == "BUY" and "downgraded to WAIT: consensus SELL" in f.explanation
    f = run(engine("BUY"), consensus="BUY", probability=prob(-0.2))
    assert f.decision == "WAIT" and "expects no positive R after costs" in f.explanation
    f = run(engine("BUY"), consensus="BUY", probability=prob(-0.2, low=True))
    assert f.decision == "BUY", "thin evidence is reported, not acted on"
    f = run(engine("BUY"), consensus="BUY", probability=prob(0.4))
    assert f.decision == "BUY" and "same as the original engine" in f.explanation
    names = [x.factor for x in f.factors]
    for required in ("strategies", "regime", "multi_timeframe", "consensus", "probability", "calibration", "news", "spread",
                     "anomalies"):
        assert required in names


def test_probability_is_only_ever_a_calibrated_estimate():
    f = run(engine("BUY"))
    assert f.evidence == "INSUFFICIENT_DATA" and f.probability is None and "INSUFFICIENT DATA" in f.explanation
    assert f.score == {"value": ENGINE.score, "label": "confluence score 0-100 (not a probability)"}
    f = run(engine("BUY", sample=12))
    assert f.evidence == "LOW_DATA_CONFIDENCE" and f.probability is None and "LOW DATA CONFIDENCE" in f.explanation
    f = run(engine("BUY", calibrated=0.58, sample=64))
    assert f.evidence == "CALIBRATED" and f.probability["value"] == 0.58 and f.probability["samples"] == 64
    f = run(engine("BUY", calibrated=0.58, sample=64), probability=prob(0.4, shadow=True))
    assert f.probability["source"] == "signal engine out-of-sample calibration", "a shadow model is never used"
    assert "not approved (shadow)" in next(x for x in f.factors if x.factor == "probability").detail
    f = run(engine("BUY"), probability=prob(0.4))
    assert f.evidence == "CALIBRATED" and f.probability["source"] == "prob_tp_before_sl:v3" and f.probability["samples"] == 240
    f = run(engine("BUY"), probability=prob(0.4, low=True))
    assert f.evidence == "LOW_DATA_CONFIDENCE"


def test_history_and_exposure_are_reported(db_clean):
    demo, _ = accounts()
    with session_scope() as db:
        db.get(TradingAccount, demo).is_active = db.get(TradingAccount, demo).trading_enabled = True
        from app.db.models_intel import PerformanceSnapshot
        now = datetime.now(UTC)
        db.add(PerformanceSnapshot(mode="DEMO", dimension="strategy", key="A", sample_size=64,
                                   metrics={"verdict": "NEGATIVE_EDGE", "average_r": -0.3}, computed_at=now))
        db.add(PerformanceSnapshot(mode="LIVE", dimension="strategy", key="A", sample_size=80,
                                   metrics={"verdict": "POSITIVE_EDGE", "average_r": 0.3}, computed_at=now))
        db.flush()
        h = fusion.historical(db, "DEMO", ["A", "B"])
        assert h == {"A": {"trades": 64, "verdict": "NEGATIVE_EDGE", "average_r": -0.3}, "B": {"trades": 0, "verdict": "NO_DATA"}}
        f = run(engine("BUY"), history=h)
        hp = next(x for x in f.factors if x.factor == "historical_performance")
        assert hp.status == "OPPOSES" and hp.effect == "explains" and f.decision == "BUY", "DEMO only; reported, not acted on"
        ex = fusion.exposure(db, "EURUSD.r", "BUY")
        assert ex["available"] is True and ex["related"] == []


def test_scanner_records_fusion_advisory_by_default_enforce_on_request(db_clean, monkeypatch):
    demo, _ = accounts()
    with session_scope() as db:
        sym = mk_symbol(db, demo)
        intel = {"consensus": cons("SELL"), "anomalies": {"found": [], "blocking": []}}
        res = replace(engine("BUY"), warnings=[])
        out = scanner._fuse(db, sym, res, intel, "CLEAR", 0.00001, ctx(), SignalConfig(), [])
        assert out["mode"] == "advisory" and out["decision"] == "WAIT" and res.decision == "BUY", "advisory keeps the original"
        monkeypatch.setenv("RR_FUSION_MODE", "enforce")
        get_settings.cache_clear()
        try:
            res = replace(engine("BUY"), warnings=[])
            out = scanner._fuse(db, sym, res, intel, "CLEAR", 0.00001, ctx(), SignalConfig(), [])
            assert res.decision == "WAIT" and res.direction == "BUY" and "enforce mode" in res.warnings[-1]
            assert out["benchmark"]["decision"] == "BUY"
            res = replace(engine("BUY"), warnings=[])
            scanner._fuse(db, sym, res, intel, "RESTRICTED", 0.00001, ctx(), SignalConfig(), [])
            assert res.decision == "NO_TRADE" and res.direction is None
        finally:
            get_settings.cache_clear()


def test_benchmark_report_counts_and_outcomes(db_clean):
    demo, _ = accounts()
    t0 = datetime.now(UTC) - timedelta(hours=1)
    with session_scope() as db:
        sym = mk_symbol(db, demo)
        for i, (bench, fused, r) in enumerate([("BUY", "WAIT", -1.0), ("BUY", "BUY", 2.0), ("WAIT", "NO_TRADE", None),
                                               ("BUY", "NO_TRADE", 1.5)]):
            ar = AnalysisResult(symbol_id=sym.id, timeframe="H1", bar_time=t0, regime="X", structure={}, strategy_outputs=[],
                                score={"fusion": {"decision": fused, "benchmark": {"decision": bench}, "evidence": "INSUFFICIENT_DATA"}})
            db.add(ar)
            db.flush()
            if r is not None:
                sig = TradingSignal(symbol_id=sym.id, analysis_id=ar.id, decision=bench, grade="A", timeframe="H1", score=70,
                                    strategies=[], regime="X", explanation="", expires_at=t0)
                db.add(sig)
                db.flush()
                db.add(TradeOrder(signal_id=sig.id, execution_id=f"f{i}", account_id=demo, symbol="EURUSD.r",
                                  action="MARKET_BUY", volume=0.1, status="CONFIRMED"))
                db.add(ClosedPosition(account_id=demo, ticket=500 + i, symbol="EURUSD.r", direction="BUY", volume=0.1,
                                      open_price=1.1, close_price=1.1, profit=r, r_multiple=r, opened_at=t0, closed_at=t0,
                                      execution_id=f"f{i}", mode="DEMO"))
        db.flush()
        rep = fusion.benchmark_report(db)
    assert rep["fused_analyses"] == 4 and rep["differ"] == 3
    assert rep["changes"] == {"BUY -> WAIT": 1, "WAIT -> NO_TRADE": 1, "BUY -> NO_TRADE": 1}
    assert rep["held_back_trades_with_outcome"] == 2 and rep["r_avoided"] == -0.5 and rep["verdict"] == "INSUFFICIENT_DATA"


def test_fusion_api(client, db_clean):
    login(client, "viewer")
    r = client.get("/api/intelligence/fusion/latest").json()
    assert r == {"mode": "advisory", "decisions": []}
    b = client.get("/api/intelligence/fusion/benchmark?days=7").json()
    assert b["mode"] == "advisory" and b["fused_analyses"] == 0 and b["verdict"] == "INSUFFICIENT_DATA"
