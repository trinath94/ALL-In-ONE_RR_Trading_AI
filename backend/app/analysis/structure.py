"""Market structure primitives used by SMC / ICT / price-action / liquidity strategies.

Causality rule: a swing point at bar i is only *known* at bar i + strength (it needs
`strength` bars to its right). Every function here returns the confirmation index so
strategies never use a swing before it could have been observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.analysis.indicators import atr


@dataclass
class Swing:
    idx: int          # bar index of the extreme
    confirmed: int    # bar index at which it becomes known
    price: float
    kind: str         # "H" or "L"


@dataclass
class StructureEvent:
    idx: int
    kind: str         # BOS | CHOCH
    direction: str    # BUY (bullish) | SELL (bearish)
    level: float
    swing_idx: int


@dataclass
class FVG:
    idx: int          # index of the third candle (gap known at its close)
    direction: str    # BUY = bullish gap (support), SELL = bearish gap (resistance)
    top: float
    bottom: float
    filled_idx: int | None = None
    inverted_idx: int | None = None   # closed through -> IFVG

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Zone:
    idx: int
    direction: str
    top: float
    bottom: float
    kind: str
    meta: dict = field(default_factory=dict)
    mitigated_idx: int | None = None
    broken_idx: int | None = None


def swings(df: pd.DataFrame, strength: int = 3) -> list[Swing]:
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    n, w = len(df), 2 * strength + 1
    out: list[Swing] = []
    if n < w:
        return out
    wh = np.lib.stride_tricks.sliding_window_view(h, w)
    wl = np.lib.stride_tricks.sliding_window_view(l, w)
    is_h = np.argmax(wh, axis=1) == strength  # first occurrence of max is the centre bar
    is_l = np.argmin(wl, axis=1) == strength
    for j in np.nonzero(is_h)[0]:
        i = j + strength
        out.append(Swing(int(i), int(i + strength), float(h[i]), "H"))
    for j in np.nonzero(is_l)[0]:
        i = j + strength
        out.append(Swing(int(i), int(i + strength), float(l[i]), "L"))
    out.sort(key=lambda s: (s.confirmed, s.idx))
    return out


def known_swings(sw: list[Swing], at: int) -> list[Swing]:
    return [s for s in sw if s.confirmed <= at]


def structure_events(df: pd.DataFrame, strength: int = 3) -> tuple[list[StructureEvent], int]:
    """Walk bars sequentially; a close beyond the last confirmed swing high/low is a break.
    Break in the same direction as prevailing trend = BOS, against it = CHOCH.
    Returns (events, final_trend) with trend 1 bull, -1 bear, 0 unknown."""
    sw = swings(df, strength)
    close = df["close"].values
    events: list[StructureEvent] = []
    trend = 0
    last_h: Swing | None = None
    last_l: Swing | None = None
    used_h: set[int] = set()
    used_l: set[int] = set()
    j = 0
    for i in range(len(df)):
        while j < len(sw) and sw[j].confirmed <= i:
            if sw[j].kind == "H":
                last_h = sw[j]
            else:
                last_l = sw[j]
            j += 1
        if last_h and last_h.idx not in used_h and close[i] > last_h.price:
            kind = "CHOCH" if trend == -1 else "BOS"
            events.append(StructureEvent(i, kind, "BUY", last_h.price, last_h.idx))
            used_h.add(last_h.idx)
            trend = 1
        elif last_l and last_l.idx not in used_l and close[i] < last_l.price:
            kind = "CHOCH" if trend == 1 else "BOS"
            events.append(StructureEvent(i, kind, "SELL", last_l.price, last_l.idx))
            used_l.add(last_l.idx)
            trend = -1
    return events, trend


def fair_value_gaps(df: pd.DataFrame, min_atr_frac: float = 0.1) -> list[FVG]:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    a = atr(df, 14).values
    out: list[FVG] = []
    for i in range(2, len(df)):
        thr = (a[i] if not np.isnan(a[i]) else 0) * min_atr_frac
        if l[i] > h[i - 2] and l[i] - h[i - 2] > thr:
            out.append(FVG(i, "BUY", float(l[i]), float(h[i - 2])))
        elif h[i] < l[i - 2] and l[i - 2] - h[i] > thr:
            out.append(FVG(i, "SELL", float(l[i - 2]), float(h[i])))
    # lifecycle: filled when price trades through the far edge; inverted when a close passes it
    for g in out:
        for k in range(g.idx + 1, len(df)):
            if g.direction == "BUY":
                if g.filled_idx is None and l[k] <= g.bottom:
                    g.filled_idx = k
                if c[k] < g.bottom:
                    g.inverted_idx = k
                    break
            else:
                if g.filled_idx is None and h[k] >= g.top:
                    g.filled_idx = k
                if c[k] > g.top:
                    g.inverted_idx = k
                    break
    return out


def displacement(df: pd.DataFrame, mult: float = 1.8, n: int = 14) -> pd.Series:
    """+1 / -1 where the candle body exceeds mult x ATR (strong institutional move)."""
    body = df["close"] - df["open"]
    a = atr(df, n)
    return np.sign(body).where(body.abs() > mult * a, 0).fillna(0)


def order_blocks(df: pd.DataFrame, strength: int = 3, lookback: int = 10) -> list[Zone]:
    """Order block = last opposite-coloured candle before a displacement leg that breaks structure."""
    o, c, h, l = (df[k].values for k in ("open", "close", "high", "low"))
    events, _ = structure_events(df, strength)
    disp = displacement(df).values
    zones: list[Zone] = []
    for ev in events:
        start = max(ev.swing_idx, ev.idx - lookback)
        if not any(disp[k] != 0 for k in range(start, ev.idx + 1)):
            continue
        rng = range(ev.idx - 1, max(start - lookback, 0) - 1, -1)
        for k in rng:
            if ev.direction == "BUY" and c[k] < o[k]:
                zones.append(Zone(ev.idx, "BUY", float(h[k]), float(l[k]), "OB", {"candle": k, "event": ev.kind}))
                break
            if ev.direction == "SELL" and c[k] > o[k]:
                zones.append(Zone(ev.idx, "SELL", float(h[k]), float(l[k]), "OB", {"candle": k, "event": ev.kind}))
                break
    for z in zones:
        for k in range(z.idx + 1, len(df)):
            if z.mitigated_idx is None and l[k] <= z.top and h[k] >= z.bottom:
                z.mitigated_idx = k
            if (z.direction == "BUY" and c[k] < z.bottom) or (z.direction == "SELL" and c[k] > z.top):
                z.broken_idx = k
                break
    return zones


def equal_levels(sw: list[Swing], kind: str, tol: float) -> list[tuple[Swing, Swing]]:
    pts = [s for s in sw if s.kind == kind]
    out = []
    for a_i in range(len(pts)):
        for b_i in range(a_i + 1, min(a_i + 6, len(pts))):
            if abs(pts[a_i].price - pts[b_i].price) <= tol:
                out.append((pts[a_i], pts[b_i]))
    return out


def dealing_range(df: pd.DataFrame, at: int, strength: int = 3, lookback: int = 100) -> tuple[float, float] | None:
    sw = [s for s in known_swings(swings(df.iloc[: at + 1], strength), at) if s.idx >= at - lookback]
    hs = [s.price for s in sw if s.kind == "H"]
    ls = [s.price for s in sw if s.kind == "L"]
    if not hs or not ls:
        return None
    return max(hs), min(ls)


def premium_discount(price: float, rng: tuple[float, float]) -> float:
    """0 = range low (deep discount), 1 = range high (deep premium)."""
    hi, lo = rng
    return (price - lo) / (hi - lo) if hi > lo else 0.5


def period_levels(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Previous completed period high/low aligned to each bar (rule: 'D','W','MS').
    Uses only fully-closed prior periods."""
    g = df.resample(rule if rule != "W" else "W-SUN", label="left", closed="left")
    agg = g.agg({"high": "max", "low": "min", "open": "first", "close": "last"}).dropna()
    prev = agg.shift(1)
    key = df.index.floor("D") if rule == "D" else None
    if rule == "D":
        mapped = prev.reindex(key)
        mapped.index = df.index
        return mapped
    return prev.reindex(df.index, method="ffill")


def session_range(df: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """High/low of the most recent completed run of bars where mask is True, aligned per bar."""
    hi = np.full(len(df), np.nan)
    lo = np.full(len(df), np.nan)
    cur_h = cur_l = np.nan
    last_h = last_l = np.nan
    in_sess = False
    m = mask.values
    H, L = df["high"].values, df["low"].values
    for i in range(len(df)):
        if m[i]:
            if not in_sess:
                cur_h, cur_l = H[i], L[i]
                in_sess = True
            else:
                cur_h, cur_l = max(cur_h, H[i]), min(cur_l, L[i])
        else:
            if in_sess:
                last_h, last_l = cur_h, cur_l
                in_sess = False
        hi[i], lo[i] = last_h, last_l
    return pd.DataFrame({"high": hi, "low": lo}, index=df.index)
