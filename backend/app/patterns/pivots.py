"""Alternating pivots (ZigZag) built on the engine's causal swing detection (``app.analysis.structure.swings``).

A pivot is known only when its swing is confirmed (``strength`` bars later). Consecutive pivots of the same kind keep
the more extreme one; legs smaller than ``min_leg_atr`` x ATR are merged away, so noise does not make patterns.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.analysis.indicators import atr
from app.analysis.structure import swings


@dataclass(frozen=True)
class Pivot:
    idx: int          # bar of the extreme
    confirmed: int    # bar at which it is known
    price: float
    kind: str         # "H" | "L"


def zigzag(df: pd.DataFrame, strength: int = 3, min_leg_atr: float = 1.0) -> list[Pivot]:
    if len(df) < 4 * strength + 20:
        return []
    a = atr(df)
    out: list[Pivot] = []
    for s in sorted(swings(df, strength), key=lambda s: (s.idx, s.kind)):
        p = Pivot(s.idx, s.confirmed, s.price, s.kind)
        if out and out[-1].kind == p.kind:
            prev = out[-1]
            if (p.kind == "H" and p.price >= prev.price) or (p.kind == "L" and p.price <= prev.price):
                out[-1] = p
            continue
        if out:
            ref = float(a.iloc[p.idx]) if p.idx < len(a) and a.iloc[p.idx] == a.iloc[p.idx] else 0.0
            if ref and abs(p.price - out[-1].price) < min_leg_atr * ref:
                continue                      # too small a leg: wait for a larger opposite move
        out.append(p)
    return out
