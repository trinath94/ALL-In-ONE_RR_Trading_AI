from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting import engine as bt
from app.signals.engine import Calibrator, MarketContext, SignalConfig, build_signal, validate_weights
from app.strategies.base import (C_LIQUIDITY, C_SMC_ICT, C_STRUCTURE, Strategy, StrategySignal)
from app.strategies import registry
from tests.synthetic import make_ohlc

NOW = datetime(2025, 6, 2, 12, tzinfo=timezone.utc)


def ctx(**kw):
    d = dict(symbol="EURUSD", timeframe="H1", price=1.1, bar_time=NOW, atr=0.001, spread=0.0001, regime="STRONG_BULL",
             regime_tags=[], htf_bias=1, sessions=["LONDON"], news_status="CLEAR")
    d.update(kw)
    return MarketContext(**d)


def sig(code, d="BUY", comp=C_SMC_ICT, strength=0.8, entry=1.1, sl=1.098, tp=1.106):
    return StrategySignal(code, d, entry, sl, tp, strength, ["test"], comp)


def test_no_signals_is_no_trade():
    r = build_signal([], ctx())
    assert r.decision == "NO_TRADE" and r.grade == "REJECTED"


def test_confluent_signals_grade_and_levels():
    s = [sig("a", comp=C_SMC_ICT, strength=1), sig("b", comp=C_STRUCTURE, strength=1),
         sig("c", comp=C_LIQUIDITY, strength=1), sig("d", comp="price_action", strength=1),
         sig("e", comp="trend", strength=1), sig("f", comp="indicator", strength=1)]
    r = build_signal(s, ctx())
    assert r.decision == "BUY" and r.grade in ("A+", "A")
    assert r.stop_loss < r.entry < r.tp1 < r.tp2 < r.tp3
    assert r.calibrated_confidence is None and any("uncalibrated" in w for w in r.warnings)


def test_conflict_produces_wait():
    r = build_signal([sig("a", strength=0.9), sig("b", d="SELL", strength=0.9, sl=1.102, tp=1.094)], ctx(htf_bias=0))
    assert r.decision == "WAIT"


def test_news_restriction_and_low_rr_reject():
    assert build_signal([sig("a", strength=1)], ctx(news_status="RESTRICTED")).decision == "NO_TRADE"
    assert build_signal([sig("a", strength=1, tp=1.1005)], ctx()).decision == "NO_TRADE"


def test_weights_validation():
    with pytest.raises(ValueError):
        validate_weights({"smc_ict": 1})
    w = validate_weights({k: 2 for k in SignalConfig().weights})
    assert sum(w.values()) == pytest.approx(1)


def test_calibrator_is_monotone_and_requires_samples():
    rng = np.random.default_rng(0)
    scores = rng.uniform(0, 100, 2000)
    wins = rng.uniform(0, 1, 2000) < scores / 120
    c = Calibrator.fit(list(scores), list(wins))
    probs = [c.predict(s)[0] for s in (10, 40, 70, 95)]
    assert probs == sorted(probs) and probs[-1] > probs[0]
    small = Calibrator.fit([50, 60], [True, False])
    assert small.predict(55)[0] is None


# ---- backtester -------------------------------------------------------------------------------
class _BuyOnce(Strategy):
    code, name, category = "test_buy_once", "test", "test"
    params = {"at": 0}
    min_bars = 10

    def detect(self, c):
        if c.df.index[-1] == self.p["at"]:
            e = c.last()
            return StrategySignal(self.code, "BUY", e, e - 0.01, e + 0.02)
        return None


@pytest.fixture
def reg_patch(monkeypatch):
    orig = registry.get
    monkeypatch.setattr(registry, "get", lambda code: _BuyOnce if code == "test_buy_once" else orig(code))


def frame(rows):
    idx = pd.date_range("2025-01-01", periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1)


def base_rows(n=20):
    return [[1.0, 1.001, 0.999, 1.0]] * n


def test_fill_next_open_with_spread_and_hits_tp(reg_patch):
    rows = base_rows() + [[1.002, 1.003, 1.001, 1.002], [1.002, 1.030, 1.001, 1.025]]
    df = frame(rows)
    spec = bt.InstrumentSpec(point=0.0001, tick_size=0.0001, tick_value=1.0)
    cfg = bt.BacktestConfig(["test_buy_once"], window=10, params={"test_buy_once": {"at": df.index[19]}},
                            costs=bt.CostModel(spread_points=2), spec=spec, sizing="fixed", fixed_lots=1.0)
    res = bt.run_backtest(df, cfg)
    t = res["trades"][0]
    assert t["entry"] == pytest.approx(1.002 + 0.0002)      # next bar open + spread (ask)
    assert t["reason"] == "TP" and t["exit"] == pytest.approx(1.02)


def test_same_bar_sl_and_tp_assumes_stop_first(reg_patch):
    rows = base_rows() + [[1.0, 1.0, 1.0, 1.0], [1.0, 1.05, 0.95, 1.0]]
    df = frame(rows)
    cfg = bt.BacktestConfig(["test_buy_once"], window=10, params={"test_buy_once": {"at": df.index[19]}},
                            costs=bt.CostModel(spread_points=0), sizing="fixed", fixed_lots=1.0,
                            spec=bt.InstrumentSpec(point=0.0001, tick_size=0.0001, tick_value=1.0))
    t = bt.run_backtest(df, cfg)["trades"][0]
    assert t["reason"] == "SL" and t["pnl"] < 0


def test_gap_through_stop_fills_at_open(reg_patch):
    rows = base_rows() + [[1.0, 1.0, 1.0, 1.0], [0.97, 0.975, 0.96, 0.97]]
    df = frame(rows)
    cfg = bt.BacktestConfig(["test_buy_once"], window=10, params={"test_buy_once": {"at": df.index[19]}},
                            costs=bt.CostModel(spread_points=0), sizing="fixed", fixed_lots=1.0,
                            spec=bt.InstrumentSpec(point=0.0001, tick_size=0.0001, tick_value=1.0))
    t = bt.run_backtest(df, cfg)["trades"][0]
    assert t["reason"] == "SL_GAP" and t["exit"] == pytest.approx(0.97) and t["r"] < -1


def test_closed_htf_excludes_forming_bar():
    df = make_ohlc(200, freq="1h")
    h4 = bt.resample(df, "H4")
    t = pd.Timestamp("2025-01-06 05:00", tz="UTC")   # H1 bar 05:00-06:00
    vis = bt.closed_htf(h4, "H4", "H1", t)
    assert vis.index[-1] == pd.Timestamp("2025-01-06 00:00", tz="UTC")  # 04:00 H4 bar not closed until 08:00


def test_real_strategy_backtest_metrics_and_tools():
    df = make_ohlc(1500, seed=21, freq="1h")
    cfg = bt.BacktestConfig(["trend_ema_20_50", "pa_engulfing"], window=200, costs=bt.CostModel(10, 3.5, 1))
    res = bt.run_backtest(df, cfg)
    m = res["metrics"]
    assert m["total_trades"] > 0 and m["winning_trades"] + m["losing_trades"] == m["total_trades"]
    assert m["final_balance"] == pytest.approx(10_000 + sum(t["pnl"] for t in res["trades"]), abs=0.05)
    mc = bt.monte_carlo(res["trades"], 10_000, runs=200)
    assert mc["max_drawdown_pct"][5] <= mc["max_drawdown_pct"][95]
    oos = bt.split_oos(df, cfg)
    assert oos["in_sample"]["trades"] and "cut_time" in oos
    sens = bt.spread_sensitivity(df, bt.BacktestConfig(["trend_ema_20_50"], window=200), 10, (1, 3))
    assert sens[1]["net_profit"] <= sens[0]["net_profit"]


def test_optimise_reports_oos_for_every_ranked_row():
    df = make_ohlc(1200, seed=4, freq="1h")
    rows = bt.optimise(df, bt.BacktestConfig(["trend_ema_20_50"], window=200), "trend_ema_20_50",
                       {"fast": [10, 20], "slow": [50, 80]}, min_trades=1, top=3)
    assert rows and all("out_of_sample" in r for r in rows)
