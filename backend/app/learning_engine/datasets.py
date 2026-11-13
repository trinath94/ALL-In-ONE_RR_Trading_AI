"""Leakage-safe dataset generation for probability models.

* A sample is created when an enabled strategy fires on the **closed** bar ``i``. Features use bars
  ``<= i`` only (same causal context the live scanner sees).
* Labels use bars ``i+1 .. i+horizon`` (triple barrier): entry at the next open plus spread, TP vs SL —
  whichever is hit first, SL first on ambiguous bars (same pessimism as the backtester). Time-outs are
  labelled separately and excluded from the TP-before-SL target.
* Splits are chronological with an **embargo** of ``horizon`` bars between train / validation / test so
  overlapping label windows cannot leak future information.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.strategies import registry
from app.strategies.base import StrategyContext
from app.strategies.quantitative import FEATURES as BASE_FEATURES
from app.strategies.quantitative import ml_features

COMPONENTS = ["market_structure", "smc_ict", "price_action", "trend", "indicator", "liquidity", "volume", "mtf",
              "session_volatility"]
SIGNAL_FEATURES = ["dir", "rr", "stop_atr", "strength", "hour_sin", "hour_cos", "adx14", "atr_rank", "n_confluent"] + \
                  [f"comp_{c}" for c in COMPONENTS]
FEATURES = BASE_FEATURES + SIGNAL_FEATURES


@dataclass
class DatasetSpec:
    strategies: list[str]
    horizon: int = 48
    window: int = 300
    spread_points: float = 10.0
    point: float = 0.00001
    step: int = 1


def build(df: pd.DataFrame, spec: DatasetSpec, symbol: str = "", timeframe: str = "H1") -> pd.DataFrame:
    base = ml_features(df)
    adx = ind.adx(df)["adx"]
    atr = ind.atr(df)
    atr_rank = (atr / df["close"]).rolling(200, min_periods=50).rank(pct=True)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    strategies = {code: registry.get(code)() for code in spec.strategies}
    rows = []
    n = len(df)
    sp = spec.spread_points * spec.point
    for i in range(spec.window, n - spec.horizon - 1, spec.step):
        ctx = StrategyContext(df.iloc[i - spec.window: i + 1], symbol, timeframe, point=spec.point)
        sigs = []
        for code, s in strategies.items():
            try:
                sig = s.evaluate(ctx)
            except Exception:  # noqa: BLE001
                sig = None
            if sig:
                sigs.append(sig)
        for sig in sigs:
            d = 1 if sig.direction == "BUY" else -1
            entry = o[i + 1] + (sp if d > 0 else 0)
            risk = abs(entry - sig.stop_loss)
            if risk <= 0 or (entry - sig.stop_loss) * d <= 0 or (sig.take_profit - entry) * d <= 0:
                continue
            label, bars_to_exit = None, None
            for j in range(i + 1, i + 1 + spec.horizon):
                lo, hi = (l[j], h[j]) if d > 0 else (l[j] + sp, h[j] + sp)
                hit_sl = lo <= sig.stop_loss if d > 0 else hi >= sig.stop_loss
                hit_tp = hi >= sig.take_profit if d > 0 else lo <= sig.take_profit
                if hit_sl:
                    label, bars_to_exit = 0, j - i
                    break
                if hit_tp:
                    label, bars_to_exit = 1, j - i
                    break
            end = i + spec.horizon
            cont = int((c[end] - entry) * d > 0)
            rr = abs(sig.take_profit - entry) / risk
            ts = df.index[i]
            row = {k: base[k].iloc[i] for k in BASE_FEATURES}
            row.update({
                "time": ts, "symbol": symbol, "timeframe": timeframe, "strategy": sig.strategy, "component": sig.component,
                "dir": d, "rr": rr, "stop_atr": risk / atr.iloc[i] if atr.iloc[i] else np.nan, "strength": sig.strength,
                "hour_sin": np.sin(2 * np.pi * ts.hour / 24), "hour_cos": np.cos(2 * np.pi * ts.hour / 24),
                "adx14": adx.iloc[i] / 100, "atr_rank": atr_rank.iloc[i],
                "n_confluent": sum(1 for x in sigs if x.direction == sig.direction),
                "y_tp_before_sl": label, "y_continuation": cont, "bars_to_exit": bars_to_exit, "timeout": label is None,
                "realized_r": (1.0 * rr if label == 1 else (-1.0 if label == 0 else ((c[end] - entry) * d) / risk)),
                "bar_index": i,
            })
            for comp in COMPONENTS:
                row[f"comp_{comp}"] = 1.0 if sig.component == comp else 0.0
            rows.append(row)
    return pd.DataFrame(rows)


def chronological_split(ds: pd.DataFrame, horizon: int, fractions=(0.6, 0.2, 0.2)) -> dict[str, pd.DataFrame]:
    """Train / validation / final-test by time with an embargo of ``horizon`` bars at each boundary."""
    ds = ds.sort_values("bar_index").reset_index(drop=True)
    if ds.empty:
        return {"train": ds, "validation": ds, "test": ds}
    bmin, bmax = ds.bar_index.min(), ds.bar_index.max()
    span = bmax - bmin
    c1 = bmin + span * fractions[0]
    c2 = bmin + span * (fractions[0] + fractions[1])
    return {
        "train": ds[ds.bar_index + horizon < c1],
        "validation": ds[(ds.bar_index >= c1) & (ds.bar_index + horizon < c2)],
        "test": ds[ds.bar_index >= c2],
    }
