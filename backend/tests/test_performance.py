"""Section 24 performance measurements (generous ceilings; the measured numbers are printed and
reported in docs/PART25_26_REPORT.md)."""
import time

import numpy as np
import pytest
from sqlalchemy import select

from app.db.session import session_scope
from app.intelligence import consensus, probability
from app.intelligence import regime as ri
from app.learning_engine import models
from app.strategies.base import StrategySignal
from tests.synthetic import make_ohlc
from tests.test_intelligence import planted

RESULTS: dict[str, float] = {}


def timed(name, fn, n=5):
    fn()                                            # warm-up
    t = time.perf_counter()
    for _ in range(n):
        fn()
    RESULTS[name] = (time.perf_counter() - t) / n * 1000
    print(f"PERF {name}: {RESULTS[name]:.1f} ms")
    return RESULTS[name]


def test_regime_intelligence_latency():
    df = make_ohlc(500, seed=1, freq="1h")
    assert timed("regime_intel_500_bars", lambda: ri.analyse(df, "CLEAR", {})) < 2000


def test_probability_inference_and_consensus_latency(db_clean):
    df = make_ohlc(600, seed=2, freq="1h")
    sig = {"direction": "BUY", "entry": float(df.close.iloc[-1]), "stop_loss": float(df.close.iloc[-1]) - 0.002,
           "take_profit": float(df.close.iloc[-1]) + 0.004, "strength": 0.6, "component": "trend"}
    with session_scope() as db:
        models.train(db, planted(seed=1), horizon=10)
        assert timed("probability_inference", lambda: probability.estimate(db, df, sig, 1, 10, 1e-5, "S", "H1", record=False), n=10) < 500
        sigs = [StrategySignal("smc_fvg", "BUY", 1.1, 1.09, 1.12, 0.7, [], "smc_ict"),
                StrategySignal("trend_ema_9_21", "BUY", 1.1, 1.09, 1.12, 0.7, [], "trend")]
        cats = {"smc_fvg": "smc", "trend_ema_9_21": "trend"}
        assert timed("consensus", lambda: consensus.run(db, sigs, cats, None, None, [], "BUY"), n=20) < 100
        db.rollback()


def test_scanner_analysis_with_intelligence_latency(stack):
    from app.db.models import Symbol
    from app.scanner.service import analyse, calibrator, enabled_strategies, strategy_stats
    from app.signals.engine import SignalConfig
    from app.workers.main import startup_checks
    fake, bridge = stack
    base = fake.copy_rates_from_pos

    def varied(name, tf, start, count):
        arr = base(name, tf, start, count)
        rng = np.random.default_rng(1)
        arr["close"] = arr["close"][0] * np.exp(np.cumsum(rng.normal(0, 0.0008, count)))
        arr["open"] = np.r_[arr["close"][0], arr["close"][:-1]]
        arr["high"] = np.maximum(arr["open"], arr["close"]) * 1.0004
        arr["low"] = np.minimum(arr["open"], arr["close"]) * 0.9996
        return arr
    fake.copy_rates_from_pos = varied
    assert startup_checks(bridge)[0]
    with session_scope() as db:
        enabled = enabled_strategies(db, "DEMO")
        stats, calib = strategy_stats(db), calibrator(db)
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        ms = timed("scanner_analyse_1_symbol_tf_all_enabled_strategies",
                   lambda: analyse(db, bridge, sym, "H1", enabled, stats, calib, SignalConfig()), n=3)
        db.rollback()
    assert ms < 15000


def test_learning_api_latency(stack):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.test_api import login, make_user
    make_user("perf", "admin")
    c = TestClient(app)
    login(c, "perf")
    for path in ("/api/learning/overview", "/api/learning/performance", "/api/intelligence/regimes",
                 "/api/intelligence/execution", "/api/intelligence/portfolio"):
        assert timed(f"GET {path}", lambda p=path: c.get(p).raise_for_status(), n=5) < 2000


def test_print_summary():
    for k, v in RESULTS.items():
        print(f"SUMMARY {k} {v:.1f}ms")


from tests.test_execution_e2e import stack  # noqa: E402,F401
_ = pytest
