"""Detection record and its lifecycle, evaluated causally on the bars after the pattern became known.

States: FORMING -> COMPLETED -> CONFIRMED -> TARGET_REACHED, or INVALIDATED / EXPIRED at any point after FORMING.
(FORMING can also end INVALIDATED when price overshoots the completion zone.)

Measurement (pattern learning): the reference entry is the **close of the bar at which the pattern became known**
(never the pivot's own price, which was not knowable then). R = |entry - invalidation|. MFE / MAE are the largest
favourable / adverse excursions in R until the outcome. On a bar that touches both the invalidation and the target,
the invalidation is assumed first (conservative). These are measurements on price bars, not trades.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

STATES = ("FORMING", "COMPLETED", "CONFIRMED", "TARGET_REACHED", "INVALIDATED", "EXPIRED")
OPEN = ("FORMING", "COMPLETED", "CONFIRMED")
CLOSED = ("TARGET_REACHED", "INVALIDATED", "EXPIRED")


@dataclass
class Detection:
    family: str                 # harmonic | elliott
    pattern: str                # gartley | bat | butterfly | crab | abcd | elliott_impulse
    direction: str              # BUY | SELL: the move the pattern anticipates
    points: list[dict]          # [{"label": "X", "idx": i, "time": iso, "price": p}, ...]
    known_idx: int              # bar at which the current state's structure became known
    state: str
    quality: float              # 0..1, how closely the ratios / guidelines fit
    ratios: dict
    levels: dict                # prz, invalidation, targets, confirmation
    rules: str                  # the rule set used, stated
    history: list[dict] = field(default_factory=list)       # [{"state", "idx", "time", "reason"}]
    measure: dict = field(default_factory=dict)            # entry, r, mfe_r, mae_r, bars, outcome
    key_points: tuple = ()      # the point indices that identify this formation (dedupe key)

    def dict(self) -> dict:
        return asdict(self)


def t(df: pd.DataFrame, i: int) -> str:
    return df.index[i].isoformat()


def follow(df: pd.DataFrame, det: Detection, start: int, *, invalidation: float, target: float,
           confirmation: float | None, expire_bars: int) -> None:
    """Advance ``det`` (state COMPLETED at ``start``) over the bars after ``start``; fills history and measurement."""
    sgn = 1 if det.direction == "BUY" else -1
    hi, lo, cl = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    entry = float(cl[start])
    r = abs(entry - invalidation)
    mfe = mae = 0.0
    state = det.state
    if r <= 0 or (entry - invalidation) * sgn <= 0:
        det.history.append({"state": "INVALIDATED", "idx": start, "time": t(df, start),
                            "reason": "price already beyond the invalidation level when the pattern became known"})
        det.state = "INVALIDATED"
        det.measure = {"entry": entry, "r": r, "mfe_r": 0.0, "mae_r": 0.0, "bars": 0, "outcome": "INVALIDATED"}
        return
    end = len(df) - 1
    outcome, bars = None, 0
    for i in range(start + 1, len(df)):
        bars = i - start
        fav = (hi[i] - entry) if sgn > 0 else (entry - lo[i])
        adv = (entry - lo[i]) if sgn > 0 else (hi[i] - entry)
        mfe, mae = max(mfe, fav / r), max(mae, adv / r)
        if (lo[i] <= invalidation) if sgn > 0 else (hi[i] >= invalidation):
            outcome, reason = "INVALIDATED", f"price reached the invalidation level {invalidation:.6g}"
        elif (hi[i] >= target) if sgn > 0 else (lo[i] <= target):
            outcome, reason = "TARGET_REACHED", f"price reached the first target {target:.6g}"
        elif state == "COMPLETED" and confirmation is not None and \
                ((cl[i] >= confirmation) if sgn > 0 else (cl[i] <= confirmation)):
            state = "CONFIRMED"
            det.history.append({"state": "CONFIRMED", "idx": i, "time": t(df, i),
                                "reason": f"close beyond the confirmation level {confirmation:.6g}"})
            continue
        elif bars >= expire_bars:
            outcome, reason = "EXPIRED", f"neither target nor invalidation within {expire_bars} bars"
        else:
            continue
        det.history.append({"state": outcome, "idx": i, "time": t(df, i), "reason": reason})
        end = i
        break
    det.state = outcome or state
    det.measure = {"entry": entry, "r": r, "mfe_r": round(float(mfe), 3), "mae_r": round(float(mae), 3), "bars": end - start,
                   "outcome": outcome}
