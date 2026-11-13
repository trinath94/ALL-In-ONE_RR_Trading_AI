import numpy as np
import pandas as pd
import pytest

from app.analysis import indicators as ind
from app.analysis import regime as rg
from app.analysis import sessions as ss
from app.analysis import structure as st
from tests.synthetic import make_ohlc


def bars(prices, start="2025-03-03 00:00", freq="1h", spread=0.0005):
    """Build OHLC where each bar's close is the given price; open = previous close."""
    p = np.asarray(prices, float)
    o = np.r_[p[0], p[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, p) + spread, "low": np.minimum(o, p) - spread, "close": p,
                         "volume": 100}, index=pd.date_range(start, periods=len(p), freq=freq, tz="UTC"))


def test_ema_matches_pandas_and_rsi_bounds():
    df = make_ohlc(500)
    assert np.allclose(ind.ema(df.close, 20).dropna(), df.close.ewm(span=20, adjust=False).mean()[19:])
    r = ind.rsi(df.close).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert (ind.atr(df).dropna() > 0).all()


@pytest.mark.parametrize("fn", [
    lambda d: ind.ema(d.close, 20), lambda d: ind.rsi(d.close), lambda d: ind.atr(d),
    lambda d: ind.macd(d.close)["hist"], lambda d: ind.adx(d)["adx"], lambda d: ind.bollinger(d.close)["upper"],
    lambda d: ind.supertrend(d)["line"], lambda d: ind.psar(d)["sar"], lambda d: ind.ichimoku(d)["span_a"],
    lambda d: ind.donchian(d)["upper"], lambda d: ind.stochastic(d)["k"], lambda d: ind.obv(d),
    lambda d: ind.cci(d), lambda d: ind.mfi(d), lambda d: ind.tsi(d.close)["tsi"], lambda d: ind.vwap_session(d),
])
def test_indicators_are_causal(fn):
    """Value at bar i must not change when future bars are appended (no look-ahead)."""
    df = make_ohlc(600, seed=3)
    full = fn(df)
    part = fn(df.iloc[:400])
    a, b = full.iloc[:400].to_numpy(float), part.to_numpy(float)
    mask = ~np.isnan(a) & ~np.isnan(b)
    assert np.allclose(a[mask], b[mask], rtol=1e-9, atol=1e-12)


def test_swings_require_confirmation():
    df = bars([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3])
    sw = st.swings(df, 3)
    hi = [s for s in sw if s.kind == "H"]
    assert hi and hi[0].idx == 4 and hi[0].confirmed == 7
    assert not st.known_swings(sw, 6) or all(s.kind != "H" for s in st.known_swings(sw, 6))


def test_bos_and_choch_sequence():
    up = [1, 2, 3, 2.5, 2, 2.6, 3.5, 4, 3.5, 3, 3.2, 4.5]
    down = [4, 3.4, 2.8, 2.6, 2.4, 2.0, 1.5]
    df = bars(up + down, spread=0.01)
    ev, trend = st.structure_events(df, 2)
    kinds = [(e.kind, e.direction) for e in ev]
    assert ("BOS", "BUY") in kinds
    assert ("CHOCH", "SELL") in kinds
    assert trend == -1


def test_fvg_detection_and_fill():
    df = pd.DataFrame({"open": [1.0, 1.0, 1.2, 1.25, 1.1], "high": [1.01, 1.2, 1.3, 1.26, 1.12],
                       "low": [0.99, 1.0, 1.1, 1.2, 1.0], "close": [1.0, 1.2, 1.25, 1.21, 1.05], "volume": 1},
                      index=pd.date_range("2025-01-01", periods=5, freq="1h", tz="UTC"))
    g = [x for x in st.fair_value_gaps(df, 0.0) if x.direction == "BUY"]
    assert g and g[0].bottom == pytest.approx(1.01) and g[0].top == pytest.approx(1.1)
    assert g[0].filled_idx == 4


def test_london_session_handles_dst():
    # London 08:00 local = 07:00 UTC in summer (BST), 08:00 UTC in winter (GMT)
    summer = pd.DatetimeIndex([pd.Timestamp("2025-07-01 07:00", tz="UTC"), pd.Timestamp("2025-07-01 06:59", tz="UTC")])
    winter = pd.DatetimeIndex([pd.Timestamp("2025-01-15 08:00", tz="UTC"), pd.Timestamp("2025-01-15 07:30", tz="UTC")])
    assert list(ss.session_mask(summer, "LONDON")) == [True, False]
    assert list(ss.session_mask(winter, "LONDON")) == [True, False]


def test_ny_killzone_shifts_across_us_dst_change():
    # US DST began 2025-03-09; UK on 2025-03-30. NY AM kill zone 07:00 NY.
    before = pd.DatetimeIndex([pd.Timestamp("2025-03-07 12:00", tz="UTC")])  # EST -> 07:00 NY
    after = pd.DatetimeIndex([pd.Timestamp("2025-03-10 11:00", tz="UTC")])   # EDT -> 07:00 NY
    assert ss.session_mask(before, "NY_AM_KZ").iloc[0]
    assert ss.session_mask(after, "NY_AM_KZ").iloc[0]
    assert not ss.session_mask(pd.DatetimeIndex([pd.Timestamp("2025-03-10 10:30", tz="UTC")]), "NY_AM_KZ").iloc[0]


def test_asia_window_wraps_midnight():
    idx = pd.DatetimeIndex([pd.Timestamp("2025-01-15 01:00", tz="UTC"),   # 20:00 NY -> in
                            pd.Timestamp("2025-01-15 04:59", tz="UTC"),   # 23:59 NY -> in
                            pd.Timestamp("2025-01-15 05:00", tz="UTC")])  # 00:00 NY -> out
    assert list(ss.session_mask(idx, "ASIA")) == [True, True, False]


def test_regime_trend_and_range():
    n = 400
    trend = bars(np.linspace(1.0, 1.5, n) + np.random.default_rng(1).normal(0, 0.0005, n), spread=0.001)
    assert rg.classify(trend).primary in ("STRONG_BULL", "WEAK_BULL")
    flat = bars(1.0 + 0.002 * np.sin(np.arange(n) / 3), spread=0.0005)
    assert rg.classify(flat).primary in ("SIDEWAYS", "CONSOLIDATION")
    assert rg.classify(trend.iloc[:30]).primary == "UNKNOWN"
