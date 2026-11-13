import collections

import numpy as np
import pandas as pd
import pytest

from app.strategies import registry
from app.strategies.base import CAP_ORDER_FLOW, StrategyContext
from tests.synthetic import make_ohlc
from tests.test_analysis import bars

SPEC_COUNTS = {"smc": 20, "ict": 20, "price_action": 30, "liquidity": 20, "trend": 15, "indicators": 25, "volume": 15,
               "sessions": 15, "fibonacci": 10, "reversal": 10, "quantitative": 20, "combinations": 15}


def test_registry_has_full_library_with_unique_codes():
    reg = registry.all_strategies()
    assert len(reg) == 215
    assert dict(collections.Counter(c.category for c in reg.values())) == SPEC_COUNTS


def test_every_strategy_has_detection_logic_and_metadata():
    for code, cls in registry.all_strategies().items():
        assert cls.detect is not registry.Strategy.detect, code
        m = cls.meta()
        assert m.name and m.description and m.code_hash, code


def test_risky_strategies_disabled_by_default():
    for code in ("quant_grid", "quant_martingale", "quant_anti_martingale", "quant_dca"):
        assert registry.get(code).meta().default_enabled is False


@pytest.fixture(scope="module")
def sweep():
    """Run every strategy over a synthetic series; collect errors and signal validity."""
    df = make_ohlc(1400, seed=11, freq="15min")
    h4 = df.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    errors, fired, invalid = [], collections.Counter(), []
    for i in range(350, 1400, 7):
        w = df.iloc[i - 350:i + 1]
        ctx = StrategyContext(w, "EURUSD", "M15", htf={"H4": h4[h4.index + pd.Timedelta(hours=4) <= w.index[-1]]})
        sigs, diag = registry.evaluate_all(ctx)
        errors += [d for d in diag if d["status"] == "error"]
        for s in sigs:
            fired[s.strategy] += 1
            if not s.valid() or not np.isfinite([s.entry, s.stop_loss, s.take_profit]).all():
                invalid.append(s.dict())
    return errors, fired, invalid


def test_no_strategy_raises(sweep):
    errors, _, _ = sweep
    assert errors == []


def test_signals_are_well_formed(sweep):
    _, fired, invalid = sweep
    assert invalid == []
    assert len(fired) >= 120, f"only {len(fired)} strategies produced a signal on synthetic data"


def test_unavailable_data_strategies_never_fire_without_source():
    df = make_ohlc(400)
    ctx = StrategyContext(df, "EURUSD", "M15")
    for code in ("vol_cvd", "vol_delta_divergence", "vol_footprint", "vol_order_flow", "vol_iceberg",
                 "vol_open_interest", "quant_hft", "quant_market_making", "quant_vol_arb", "quant_ml", "ict_smt"):
        s = registry.get(code)()
        ok, why = s.available(ctx)
        assert not ok and "unavailable" in why
        assert s.evaluate(ctx) is None


def test_order_flow_strategy_runs_when_data_is_supplied():
    df = make_ohlc(400)
    flow = pd.DataFrame({"buy_volume": 100.0, "sell_volume": 20.0}, index=df.index)
    ctx = StrategyContext(df, "X", "M15", data_caps={"tick_volume", CAP_ORDER_FLOW}, aux={CAP_ORDER_FLOW: flow})
    sig = registry.get("vol_order_flow")().evaluate(ctx)
    assert sig is not None and sig.direction == "BUY"


def test_ema_cross_fires_exactly_on_cross_bar():
    p = list(np.linspace(1.2, 1.0, 120)) + list(np.linspace(1.0, 1.15, 40))
    df = bars(p, spread=0.0003)
    s = registry.get("trend_ema_9_21")()
    hits = [i for i in range(80, len(df)) if (sig := s.evaluate(StrategyContext(df.iloc[: i + 1]))) and sig.direction == "BUY"]
    assert len(hits) == 1
    i = hits[0]
    from app.analysis.indicators import ema
    f, sl = ema(df.close, 9), ema(df.close, 21)
    assert f.iloc[i - 1] <= sl.iloc[i - 1] and f.iloc[i] > sl.iloc[i]


def test_liquidity_sweep_reversal():
    p = [1.0, 1.01, 1.02, 1.03, 1.02, 1.01, 1.0, 0.99, 1.0, 1.01, 1.0] * 8
    df = bars(p, spread=0.001)
    last = df.index[-1] + pd.Timedelta(hours=1)
    sh = max(x.price for x in StrategyContext(df).known_swings() if x.kind == "H")
    sweep = pd.DataFrame({"open": [1.0], "high": [sh + 0.01], "low": [0.995], "close": [1.0], "volume": [100]}, index=[last])
    ctx = StrategyContext(pd.concat([df, sweep]))
    sig = registry.get("smc_liquidity_sweep")().evaluate(ctx)
    assert sig and sig.direction == "SELL" and sig.stop_loss > sh


def test_double_top_confirmation():
    up, down = list(np.linspace(1.0, 1.10, 15)), list(np.linspace(1.10, 1.05, 8))
    p = up + down + list(np.linspace(1.05, 1.1, 8)) + list(np.linspace(1.1, 1.04, 10))
    df = bars([1.0] * 40 + p, spread=0.004)
    s = registry.get("pa_double_top")()
    hits = [s.evaluate(StrategyContext(df.iloc[: i + 1])) for i in range(60, len(df))]
    sigs = [h for h in hits if h]
    assert sigs and sigs[0].direction == "SELL"


def test_engulfing_after_decline():
    p = list(np.linspace(1.10, 1.05, 70))
    df = bars(p, spread=0.0002)
    t = df.index[-1]
    extra = pd.DataFrame({"open": [1.0505, 1.0490], "high": [1.0507, 1.0530], "low": [1.0488, 1.0485],
                          "close": [1.0490, 1.0525], "volume": [100, 100]},
                         index=[t + pd.Timedelta(hours=1), t + pd.Timedelta(hours=2)])
    sig = registry.get("pa_engulfing")().evaluate(StrategyContext(pd.concat([df, extra])))
    assert sig and sig.direction == "BUY"


def test_strategies_only_use_supplied_window():
    """Evaluating at bar i on df[:i+1] vs a frame with future bars must differ only by input,
    i.e. strategies cannot reach beyond ctx.df (structural no-look-ahead)."""
    df = make_ohlc(800, seed=5)
    a = registry.evaluate_all(StrategyContext(df.iloc[:500]))[0]
    b = registry.evaluate_all(StrategyContext(df.iloc[:500].copy()))[0]
    assert [x.dict() for x in a] == [x.dict() for x in b]
