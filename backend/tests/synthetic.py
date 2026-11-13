"""Synthetic OHLC generator — TEST FIXTURE ONLY, never used by production code paths."""
import numpy as np
import pandas as pd


def make_ohlc(n=1500, seed=7, start="2025-01-06", freq="15min", drift=0.0, vol=0.0008, price=1.1):
    rng = np.random.default_rng(seed)
    # regime-switching returns so that trends and ranges both occur
    regime = np.sign(np.sin(np.arange(n) / 120.0))
    r = rng.normal(drift + regime * vol * 0.15, vol, n)
    close = price * np.exp(np.cumsum(r))
    open_ = np.r_[price, close[:-1]]
    spread = np.abs(rng.normal(0, vol * 0.7, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.7, n)) * close
    vol_ = rng.integers(100, 2000, n)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol_}, index=idx)
