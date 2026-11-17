"""Conservative Elliott impulse labelling on alternating pivots.

Only the three hard rules decide whether a five-wave impulse count is valid (0-1-2-3-4-5, pivots alternating):
1. wave 2 does not retrace beyond the start of wave 1;
2. wave 3 is not the shortest of waves 1, 3 and 5;
3. wave 4 does not enter wave 1's price territory (no overlap; diagonals are not labelled).
Progression is also required (wave 3 ends beyond wave 1, wave 5 beyond wave 3).

The usual Fibonacci *guidelines* (wave 2 retraces 50-61.8 % of wave 1, wave 3 at least 1.618 x wave 1, wave 4
retraces about 38.2 % of wave 3) only set the quality. Elliott counts are subjective, and several may be valid on the
same bars; each record says so. A count is a description, never a trading instruction.

* **FORMING** (waves 0-4 valid, wave 5 not yet confirmed): expects wave 5 in the impulse direction. Invalidation: a
  close back inside wave 1's territory (beyond the end of wave 1). Target: beyond the end of wave 3.
* **COMPLETED** (all five waves): expects a correction against the impulse. Confirmed by a close beyond the end of
  wave 4. Invalidated by a new extreme beyond wave 5 (the fifth wave was not over). Target: 38.2 % retracement of the
  whole impulse. Expires after 40 bars.
"""
from __future__ import annotations

import pandas as pd

from app.patterns.lifecycle import Detection, follow, t
from app.patterns.pivots import Pivot

VERSION = "elliott-1.0"
EXPIRE_BARS = 40
LOOKBACK_BARS = 200
NOTE = "rule-satisfying count; Elliott counts are subjective and other valid counts may exist"


def _rules(p: list[Pivot]) -> tuple[bool, dict]:
    """Hard rules on 5 or 6 pivots of one impulse. Returns (valid, details)."""
    up = p[1].price > p[0].price
    s = 1 if up else -1
    w = [s * (p[i + 1].price - p[i].price) for i in range(len(p) - 1)]       # signed so impulse legs are positive
    d = {"wave2_holds_start": s * (p[2].price - p[0].price) > 0,
         "wave3_beyond_wave1": s * (p[3].price - p[1].price) > 0,
         "no_overlap_wave4_wave1": s * (p[4].price - p[1].price) > 0 if len(p) > 4 else None}
    if len(p) == 6:
        d["wave5_beyond_wave3"] = s * (p[5].price - p[3].price) > 0
        d["wave3_not_shortest"] = not (w[2] < w[0] and w[2] < w[4])
    else:
        d["wave3_not_shortest"] = True                         # wave 5 not known yet; checked when it is
    ok = all(v for v in d.values() if v is not None)
    return ok and all(x > 0 for x in w[0::2]), d


def _guidelines(p: list[Pivot]) -> tuple[float, dict]:
    w1, w2, w3 = abs(p[1].price - p[0].price), abs(p[2].price - p[1].price), abs(p[3].price - p[2].price)
    w4 = abs(p[4].price - p[3].price) if len(p) > 4 else None
    g = {"wave2_retrace_50_618": w1 > 0 and 0.45 <= w2 / w1 <= 0.68,
         "wave3_ge_1618_wave1": w1 > 0 and w3 / w1 >= 1.55}
    if w4 is not None:
        g["wave4_retrace_about_382"] = w3 > 0 and 0.28 <= w4 / w3 <= 0.5
    return round(0.5 + 0.5 * sum(g.values()) / len(g), 3), g


def detect(df: pd.DataFrame, piv: list[Pivot]) -> list[Detection]:
    n = len(df)
    out: list[Detection] = []
    for i in range(len(piv) - 4):
        p = piv[i:i + 6]
        if p[min(len(p) - 1, 4)].idx < n - LOOKBACK_BARS:
            continue
        if len(p) < 5:
            continue
        five = p[:5]
        ok, rules = _rules(five)
        if not ok:
            continue
        up = p[1].price > p[0].price
        s = 1 if up else -1
        q, guide = _guidelines(five)
        pts = [{"label": str(k), "idx": x.idx, "time": t(df, x.idx), "price": x.price, "confirmed_idx": x.confirmed}
               for k, x in enumerate(five)]
        det = Detection("elliott", "elliott_impulse", "BUY" if up else "SELL", pts, five[4].confirmed, "FORMING", q,
                        {"rules": rules, "guidelines": guide},
                        {"invalidation": five[1].price, "targets": [five[3].price]},
                        f"{VERSION}: {NOTE}",
                        history=[{"state": "FORMING", "idx": five[4].confirmed, "time": t(df, five[4].confirmed),
                                  "reason": "waves 0-4 satisfy the hard rules; wave 5 expected"}],
                        key_points=tuple(x.idx for x in five))
        if len(p) == 6:
            ok6, rules6 = _rules(p)
            if not ok6:
                det.state = "INVALIDATED"
                det.ratios["rules"] = rules6
                det.history.append({"state": "INVALIDATED", "idx": p[5].confirmed, "time": t(df, p[5].confirmed),
                                    "reason": "the fifth wave broke a hard rule (" +
                                              ", ".join(k for k, v in rules6.items() if v is False) + ")"})
                det.measure = {"outcome": "INVALIDATED", "completed": False}
                out.append(det)
                continue
            q, guide = _guidelines(p)
            det.points.append({"label": "5", "idx": p[5].idx, "time": t(df, p[5].idx), "price": p[5].price,
                               "confirmed_idx": p[5].confirmed})
            det.ratios, det.quality, det.state, det.known_idx = {"rules": rules6, "guidelines": guide}, q, \
                "COMPLETED", p[5].confirmed
            det.direction = "SELL" if up else "BUY"                  # the correction the completed impulse implies
            det.history.append({"state": "COMPLETED", "idx": p[5].confirmed, "time": t(df, p[5].confirmed),
                                "reason": "five waves satisfy the hard rules; a correction is expected"})
            inv = p[5].price
            target = p[5].price - s * 0.382 * abs(p[5].price - p[0].price)
            det.levels = {"invalidation": inv, "targets": [round(target, 8)], "confirmation": p[4].price}
            follow(df, det, p[5].confirmed, invalidation=inv, target=target, confirmation=p[4].price,
                   expire_bars=EXPIRE_BARS)
        else:
            # forming: a close back inside wave 1's territory breaks rule 3
            closes = df["close"].to_numpy(float)[five[4].confirmed + 1:]
            bad = [j for j, c in enumerate(closes) if s * (c - five[1].price) < 0]
            if bad:
                k = five[4].confirmed + 1 + bad[0]
                det.state = "INVALIDATED"
                det.history.append({"state": "INVALIDATED", "idx": k, "time": t(df, k),
                                    "reason": "close inside wave 1's territory (wave 4 overlap)"})
                det.measure = {"outcome": "INVALIDATED", "completed": False}
        out.append(det)
    return out
