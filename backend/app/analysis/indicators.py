"""Technical indicators computed with pandas/numpy.

All functions are causal: the value at bar *i* uses only bars <= i, so they are safe
for backtesting (no look-ahead). Input frames use columns
open, high, low, close, volume (broker tick volume unless real volume is available).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    pc = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr, index=df.index)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = rma(d.clip(lower=0), n)
    dn = rma(-d.clip(upper=0), n)
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3, smooth: int = 3) -> pd.DataFrame:
    ll = df["low"].rolling(k).min()
    hh = df["high"].rolling(k).max()
    raw = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    kline = raw.rolling(smooth).mean()
    return pd.DataFrame({"k": kline, "d": kline.rolling(d).mean()})


def bollinger(s: pd.Series, n: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * sd, "lower": mid - mult * sd,
                         "width": (2 * mult * sd) / mid})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 1.5) -> pd.DataFrame:
    mid = ema(df["close"], n)
    a = atr(df, n)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * a, "lower": mid - mult * a})


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Channel of the *previous* n bars (excludes current bar to allow breakout tests)."""
    up = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    return pd.DataFrame({"upper": up, "lower": lo, "mid": (up + lo) / 2})


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = rma(true_range(df), n)
    pdi = 100 * rma(pd.Series(plus_dm, index=df.index), n) / tr
    mdi = 100 * rma(pd.Series(minus_dm, index=df.index), n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": rma(dx, n), "pdi": pdi, "mdi": mdi})


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    m = tp.rolling(n).mean()
    md = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - m) / (0.015 * md.replace(0, np.nan))


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mf = tp * df["volume"]
    pos = mf.where(tp > tp.shift(1), 0.0).rolling(n).sum()
    neg = mf.where(tp < tp.shift(1), 0.0).rolling(n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["close"].diff()).fillna(0)
    return (sign * df["volume"]).cumsum()


def vwap_session(df: pd.DataFrame, session_key: pd.Series | None = None) -> pd.Series:
    """VWAP resetting each UTC day (or per provided session key)."""
    key = session_key if session_key is not None else df.index.floor("D")
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(key).cumsum()
    vv = df["volume"].groupby(key).cumsum()
    return pv / vv.replace(0, np.nan)


def anchored_vwap(df: pd.DataFrame, anchor_pos: int) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    out = pd.Series(np.nan, index=df.index)
    sub_pv = (tp.iloc[anchor_pos:] * df["volume"].iloc[anchor_pos:]).cumsum()
    sub_v = df["volume"].iloc[anchor_pos:].cumsum().replace(0, np.nan)
    out.iloc[anchor_pos:] = (sub_pv / sub_v).values
    return out


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    a = atr(df, n).values
    hl2 = ((df["high"] + df["low"]) / 2).values
    close = df["close"].values
    up = hl2 - mult * a
    dn = hl2 + mult * a
    trend = np.ones(len(df))
    fu, fd = up.copy(), dn.copy()
    for i in range(1, len(df)):
        if np.isnan(a[i]):
            continue
        fu[i] = max(up[i], fu[i - 1]) if close[i - 1] > fu[i - 1] and not np.isnan(fu[i - 1]) else up[i]
        fd[i] = min(dn[i], fd[i - 1]) if close[i - 1] < fd[i - 1] and not np.isnan(fd[i - 1]) else dn[i]
        prev_fd, prev_fu = fd[i - 1], fu[i - 1]
        if not np.isnan(prev_fd) and close[i] > prev_fd:
            trend[i] = 1
        elif not np.isnan(prev_fu) and close[i] < prev_fu:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
    line = np.where(trend == 1, fu, fd)
    return pd.DataFrame({"trend": trend, "line": line}, index=df.index)


def psar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.DataFrame:
    high, low = df["high"].values, df["low"].values
    n = len(df)
    sar = np.full(n, np.nan)
    trend = np.zeros(n)
    if n < 3:
        return pd.DataFrame({"sar": sar, "trend": trend}, index=df.index)
    bull = high[1] > high[0]
    af = step
    ep = high[1] if bull else low[1]
    sar[1] = low[0] if bull else high[0]
    trend[1] = 1 if bull else -1
    for i in range(2, n):
        prev = sar[i - 1]
        s = prev + af * (ep - prev)
        if bull:
            s = min(s, low[i - 1], low[i - 2])
            if low[i] < s:
                bull, s, ep, af = False, ep, low[i], step
            else:
                if high[i] > ep:
                    ep, af = high[i], min(af + step, max_step)
        else:
            s = max(s, high[i - 1], high[i - 2])
            if high[i] > s:
                bull, s, ep, af = True, ep, high[i], step
            else:
                if low[i] < ep:
                    ep, af = low[i], min(af + step, max_step)
        sar[i] = s
        trend[i] = 1 if bull else -1
    return pd.DataFrame({"sar": sar, "trend": trend}, index=df.index)


def ichimoku(df: pd.DataFrame, t: int = 9, k: int = 26, s: int = 52) -> pd.DataFrame:
    """Ichimoku without forward displacement (senkou values are aligned to the bar
    at which they become visible, i.e. shifted forward by k — causal)."""
    mid = lambda n: (df["high"].rolling(n).max() + df["low"].rolling(n).min()) / 2  # noqa: E731
    tenkan, kijun = mid(t), mid(k)
    span_a = ((tenkan + kijun) / 2).shift(k)
    span_b = mid(s).shift(k)
    return pd.DataFrame({"tenkan": tenkan, "kijun": kijun, "span_a": span_a, "span_b": span_b})


def aroon(df: pd.DataFrame, n: int = 25) -> pd.DataFrame:
    up = df["high"].rolling(n + 1).apply(lambda x: 100 * np.argmax(x) / n, raw=True)
    dn = df["low"].rolling(n + 1).apply(lambda x: 100 * np.argmin(x) / n, raw=True)
    return pd.DataFrame({"up": up, "down": dn})


def tsi(s: pd.Series, long: int = 25, short: int = 13, signal: int = 7) -> pd.DataFrame:
    m = s.diff()
    num = m.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    den = m.abs().ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    line = 100 * num / den.replace(0, np.nan)
    return pd.DataFrame({"tsi": line, "signal": line.ewm(span=signal, adjust=False).mean()})


def elder_ray(df: pd.DataFrame, n: int = 13) -> pd.DataFrame:
    e = ema(df["close"], n)
    return pd.DataFrame({"bull": df["high"] - e, "bear": df["low"] - e, "ema": e})


def pivots_classic(prev_high: float, prev_low: float, prev_close: float) -> dict[str, float]:
    p = (prev_high + prev_low + prev_close) / 3
    r = prev_high - prev_low
    return {"P": p, "R1": 2 * p - prev_low, "S1": 2 * p - prev_high, "R2": p + r, "S2": p - r,
            "R3": prev_high + 2 * (p - prev_low), "S3": prev_low - 2 * (prev_high - p)}


def pivots_camarilla(prev_high: float, prev_low: float, prev_close: float) -> dict[str, float]:
    r = prev_high - prev_low
    return {"R4": prev_close + r * 1.1 / 2, "R3": prev_close + r * 1.1 / 4,
            "S3": prev_close - r * 1.1 / 4, "S4": prev_close - r * 1.1 / 2}


def volume_profile(df: pd.DataFrame, bins: int = 30) -> dict:
    """Volume-at-price histogram using each bar's typical price (approximation for OHLC data).
    Returns POC and value area (70%)."""
    tp = ((df["high"] + df["low"] + df["close"]) / 3).values
    v = df["volume"].values.astype(float)
    if len(tp) == 0 or np.nansum(v) == 0:
        return {"poc": None, "vah": None, "val": None}
    hist, edges = np.histogram(tp, bins=bins, weights=v)
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(np.argmax(hist))
    total = hist.sum()
    lo = hi = poc_i
    acc = hist[poc_i]
    while acc < 0.7 * total and (lo > 0 or hi < len(hist) - 1):
        left = hist[lo - 1] if lo > 0 else -1
        right = hist[hi + 1] if hi < len(hist) - 1 else -1
        if right >= left:
            hi += 1
            acc += hist[hi]
        else:
            lo -= 1
            acc += hist[lo]
    return {"poc": float(centers[poc_i]), "vah": float(edges[hi + 1]), "val": float(edges[lo])}


def linreg_slope(s: pd.Series, n: int) -> pd.Series:
    x = np.arange(n, dtype=float)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    return s.rolling(n).apply(lambda y: ((x - xm) * (y - y.mean())).sum() / denom, raw=True)


def zscore(s: pd.Series, n: int) -> pd.Series:
    return (s - s.rolling(n).mean()) / s.rolling(n).std(ddof=0).replace(0, np.nan)
