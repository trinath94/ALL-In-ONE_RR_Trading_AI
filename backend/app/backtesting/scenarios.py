"""Scenario simulation and counterfactual alternatives (Parts 27-30 Phase 7, Parts 30.5 and 30.6).

Pure computation on price bars. Nothing here can place, modify or cancel an order, and the results feed no trading
decision; they are research records (``simulation_runs``).

**Trade path:** a position (direction, entry, stop, target) is walked bar by bar over the bars after entry. A bar
that reaches both the stop and the target counts as the stop (conservative). An open price beyond the stop fills at
that open (a gap). Spread and slippage are charged against the result. The result is in R, where R is the distance
from entry to the original stop.

**Scenarios** (a stress test of one setup, a "digital twin"):
* **base:** the bars as they are;
* **continuation** and **reversal:** the path's moves from the entry, kept as they are or mirrored against the
  position;
* **volatility_x2:** the path's deviations from the entry doubled;
* **spread_x3** and **slippage_shock:** costs tripled, and 0.5 R of slippage;
* **gap_through_stop:** the first bar opens 0.5 R beyond the stop.

**Counterfactuals** (what else could have been done, on the bars that actually followed):
* alternative stops (0.5 to 2 times the distance) and targets (1 to 3 R);
* exit timing (6 to 48 bars);
* alternative limit entries (0.25 and 0.5 R better, filled only if price trades there).

They are **hindsight**: the best alternative is labelled as such and must not be read as a rule without separate
out-of-sample validation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Setup:
    direction: str
    entry: float
    stop: float
    target: float
    spread: float = 0.0          # price units, charged once
    slippage: float = 0.0        # price units, on entry and on a stop exit
    max_bars: int = 0            # 0 = until the data ends


def walk(bars: pd.DataFrame, s: Setup) -> dict:
    sgn = 1 if s.direction == "BUY" else -1
    r = abs(s.entry - s.stop)
    if r <= 0 or (s.entry - s.stop) * sgn <= 0:
        return {"outcome": "INVALID", "r": None, "bars": 0, "reason": "stop on the wrong side of the entry"}
    o, h, lo, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    entry = s.entry + sgn * s.slippage
    mfe = mae = 0.0
    n = len(bars) if not s.max_bars else min(len(bars), s.max_bars)
    for i in range(n):
        if (o[i] - s.stop) * sgn <= 0:
            exit_px, outcome = o[i] - sgn * s.slippage, "STOP_GAP"
        elif (lo[i] <= s.stop) if sgn > 0 else (h[i] >= s.stop):
            exit_px, outcome = s.stop - sgn * s.slippage, "STOP"
        elif (h[i] >= s.target) if sgn > 0 else (lo[i] <= s.target):
            exit_px, outcome = s.target, "TARGET"
        else:
            fav = (h[i] - entry) if sgn > 0 else (entry - lo[i])
            adv = (entry - lo[i]) if sgn > 0 else (h[i] - entry)
            mfe, mae = max(mfe, fav / r), max(mae, adv / r)
            continue
        res = ((exit_px - entry) * sgn - s.spread) / r
        return {"outcome": outcome, "r": round(res, 3), "bars": i + 1, "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
    if n == 0:
        return {"outcome": "NO_DATA", "r": None, "bars": 0}
    res = ((c[n - 1] - entry) * sgn - s.spread) / r
    return {"outcome": "TIME" if s.max_bars and n == s.max_bars else "OPEN_AT_END", "r": round(res, 3), "bars": n,
            "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}


def _transform(bars: pd.DataFrame, entry: float, f) -> pd.DataFrame:
    out = bars.copy()
    for k in ("open", "high", "low", "close"):
        out[k] = entry + f(bars[k].to_numpy(float) - entry)
    hi, lo = out[["open", "high", "low", "close"]].max(axis=1), out[["open", "high", "low", "close"]].min(axis=1)
    out["high"], out["low"] = hi, lo
    return out


def scenarios(bars: pd.DataFrame, s: Setup) -> list[dict]:
    r = abs(s.entry - s.stop)
    sgn = 1 if s.direction == "BUY" else -1
    gap = bars.copy()
    if len(gap):
        g = s.stop - sgn * 0.5 * r
        gap.iloc[0, gap.columns.get_loc("open")] = g
        gap.iloc[0, gap.columns.get_loc("low")] = min(gap.iloc[0]["low"], g)
        gap.iloc[0, gap.columns.get_loc("high")] = max(gap.iloc[0]["high"], g)
    cases = [
        ("base", bars, s, "the bars as they are"),
        ("continuation", _transform(bars, s.entry, lambda d: sgn * np.abs(d)), s, "every move from the entry in the position's favour"),
        ("reversal", _transform(bars, s.entry, lambda d: -d), s, "the path mirrored against the position"),
        ("volatility_x2", _transform(bars, s.entry, lambda d: 2 * d), s, "deviations from the entry doubled"),
        ("spread_x3", bars, Setup(**{**asdict(s), "spread": 3 * s.spread}), "spread cost tripled"),
        ("slippage_shock", bars, Setup(**{**asdict(s), "slippage": s.slippage + 0.5 * r}), "0.5 R slippage on entry and stop"),
        ("gap_through_stop", gap, s, "first bar opens 0.5 R beyond the stop"),
    ]
    return [{"scenario": name, "description": desc, **walk(b, st)} for name, b, st, desc in cases]


def counterfactuals(bars: pd.DataFrame, s: Setup) -> dict:
    r = abs(s.entry - s.stop)
    sgn = 1 if s.direction == "BUY" else -1
    actual = walk(bars, s)
    rows = []
    for k in (0.5, 1.0, 1.5, 2.0):
        for t in (1.0, 1.5, 2.0, 3.0):
            alt = Setup(**{**asdict(s), "stop": s.entry - sgn * k * r, "target": s.entry + sgn * t * k * r})
            res = walk(bars, alt)
            # in the original R so alternatives are comparable
            rr = None if res["r"] is None else round(res["r"] * k, 3)
            rows.append({"kind": "stop_target", "stop_x": k, "target_r": t, "outcome": res["outcome"], "r": rr})
    for mb in (6, 12, 24, 48):
        res = walk(bars, Setup(**{**asdict(s), "max_bars": mb}))
        rows.append({"kind": "exit_timing", "max_bars": mb, "outcome": res["outcome"], "r": res["r"]})
    lo, hi = bars["low"].to_numpy(float), bars["high"].to_numpy(float)
    for better in (0.25, 0.5):
        px = s.entry - sgn * better * r
        hit = np.nonzero((lo <= px) if sgn > 0 else (hi >= px))[0]
        if not len(hit):
            rows.append({"kind": "limit_entry", "better_r": better, "outcome": "NOT_FILLED", "r": 0.0})
            continue
        i = int(hit[0])
        res = walk(bars.iloc[i:], Setup(**{**asdict(s), "entry": px}))
        rr = None if res["r"] is None else round(res["r"] * abs(px - s.stop) / r, 3)
        rows.append({"kind": "limit_entry", "better_r": better, "filled_after_bars": i, "outcome": res["outcome"], "r": rr})
    scored = [x for x in rows if x["r"] is not None]
    best = max(scored, key=lambda x: x["r"]) if scored else None
    return {"actual": actual, "alternatives": rows, "best_in_hindsight": best,
            "note": "hindsight on the bars that followed: not a rule without separate out-of-sample validation"}
