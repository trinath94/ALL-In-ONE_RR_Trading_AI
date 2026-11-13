"""Market regime classification.

Combines ADX trend strength, EMA slope, ATR percentile (volatility), Bollinger width
squeeze, structure trend and a variance-ratio (Lo-MacKinlay style) statistic for
mean-reversion vs persistence. Output is deterministic and causal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.analysis.structure import structure_events

REGIMES = [
    "STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "SIDEWAYS", "CONSOLIDATION",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "BREAKOUT", "REVERSAL",
]


@dataclass
class RegimeResult:
    primary: str
    tags: list[str]
    adx: float | None
    ema_slope: float | None
    atr_pct: float | None
    bb_width_pct: float | None
    variance_ratio: float | None
    structure_trend: int

    def dict(self) -> dict:
        return asdict(self)


def variance_ratio(close: pd.Series, q: int = 4) -> float | None:
    r = np.log(close).diff().dropna()
    if len(r) < q * 10:
        return None
    v1 = r.var(ddof=1)
    rq = np.log(close).diff(q).dropna()
    vq = rq.var(ddof=1)
    return float(vq / (q * v1)) if v1 > 0 else None


def percentile_rank(s: pd.Series, window: int = 200) -> float | None:
    w = s.dropna().iloc[-window:]
    if len(w) < 20:
        return None
    return float((w < w.iloc[-1]).mean())


def classify(df: pd.DataFrame) -> RegimeResult:
    if len(df) < 60:
        return RegimeResult("UNKNOWN", ["INSUFFICIENT_DATA"], None, None, None, None, None, 0)
    a = ind.adx(df)
    adx_v = float(a["adx"].iloc[-1]) if not np.isnan(a["adx"].iloc[-1]) else None
    e50 = ind.ema(df["close"], 50)
    a14 = ind.atr(df)
    slope = ind.linreg_slope(e50, 10).iloc[-1]
    slope_atr = float(slope / a14.iloc[-1]) if a14.iloc[-1] and not np.isnan(slope) else None
    atr_pct = percentile_rank(a14 / df["close"])
    bbw = ind.bollinger(df["close"])["width"]
    bbw_pct = percentile_rank(bbw)
    vr = variance_ratio(df["close"].iloc[-300:])
    events, st = structure_events(df.iloc[-300:])

    tags: list[str] = []
    direction = 0
    if slope_atr is not None:
        direction = 1 if slope_atr > 0.02 else (-1 if slope_atr < -0.02 else 0)
    if direction == 0 and st != 0:
        direction = st

    if adx_v is not None and adx_v >= 25 and direction != 0:
        primary = "STRONG_BULL" if direction > 0 else "STRONG_BEAR"
    elif adx_v is not None and adx_v >= 18 and direction != 0:
        primary = "WEAK_BULL" if direction > 0 else "WEAK_BEAR"
    elif bbw_pct is not None and bbw_pct < 0.2:
        primary = "CONSOLIDATION"
    else:
        primary = "SIDEWAYS"

    if atr_pct is not None and atr_pct > 0.85:
        tags.append("HIGH_VOLATILITY")
    if atr_pct is not None and atr_pct < 0.15:
        tags.append("LOW_VOLATILITY")
    recent = [e for e in events if e.idx >= len(df.iloc[-300:]) - 5]
    if bbw_pct is not None and len(bbw.dropna()) > 6:
        prev_pct = float((bbw.dropna().iloc[-200:] < bbw.dropna().iloc[-6]).mean())
        if prev_pct < 0.2 and bbw_pct > 0.5:
            tags.append("BREAKOUT")
    if any(e.kind == "CHOCH" for e in recent):
        tags.append("REVERSAL")
    if vr is not None:
        tags.append("MEAN_REVERTING" if vr < 0.85 else ("PERSISTENT" if vr > 1.15 else "RANDOM_WALK"))
    return RegimeResult(primary, tags, adx_v, slope_atr, atr_pct, bbw_pct, vr, st)
