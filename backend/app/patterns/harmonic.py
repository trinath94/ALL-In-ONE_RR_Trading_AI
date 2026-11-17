"""Harmonic patterns: Gartley, Bat, Butterfly, Crab (XABCD) and AB=CD, on alternating pivots.

Ratios are the commonly published ones. Each leg is the price distance between consecutive pivots:
AB/XA, BC/AB, CD/BC, AD/XA (AD: from A to the completion point D). Ranges are widened by ``TOL`` (3 %).

* **FORMING:** X, A, B, C fit and the completion zone (PRZ) is non-empty. The PRZ is the intersection of the D
  projected from AD/XA and the D projected from CD/BC. Price overshooting the PRZ invalidates it.
* **COMPLETED:** a confirmed pivot D lies inside the ratios.
* **After D:**
  * invalidation: beyond X for Gartley and Bat; 0.272 x CD beyond D for Butterfly, Crab and AB=CD;
  * targets: 38.2 % and 61.8 % of AD (of CD for AB=CD);
  * confirmation: a close 23.6 % of the way back;
  * expiry: after 30 bars.
"""
from __future__ import annotations

import pandas as pd

from app.patterns.lifecycle import Detection, follow, t
from app.patterns.pivots import Pivot

TOL = 0.03
EXPIRE_BARS = 30
LOOKBACK_BARS = 150
VERSION = "harmonic-1.0"
# ratio -> (low, high, ideal or None)
SPECS = {
    "gartley": {"ab_xa": (0.568, 0.668, 0.618), "bc_ab": (0.382, 0.886, None), "cd_bc": (1.13, 1.618, None),
                "ad_xa": (0.736, 0.836, 0.786)},
    "bat": {"ab_xa": (0.382, 0.5, None), "bc_ab": (0.382, 0.886, None), "cd_bc": (1.618, 2.618, None),
            "ad_xa": (0.836, 0.936, 0.886)},
    "butterfly": {"ab_xa": (0.736, 0.836, 0.786), "bc_ab": (0.382, 0.886, None), "cd_bc": (1.618, 2.618, None),
                  "ad_xa": (1.27, 1.618, None)},
    "crab": {"ab_xa": (0.382, 0.618, None), "bc_ab": (0.382, 0.886, None), "cd_bc": (2.24, 3.618, None),
             "ad_xa": (1.568, 1.668, 1.618)},
}
ABCD = {"bc_ab": (0.382, 0.886, None), "cd_ab": (0.9, 1.1, 1.0), "cd_bc": (1.13, 2.618, None)}
STOP_BEYOND_X = ("gartley", "bat")


def _fits(ratios: dict, spec: dict) -> float | None:
    """Quality 0..1 if every ratio is inside its (widened) range, else None."""
    devs = []
    for k, (lo, hi, ideal) in spec.items():
        v = ratios.get(k)
        if v is None or not (lo * (1 - TOL) <= v <= hi * (1 + TOL)):
            return None
        mid = ideal if ideal is not None else (lo + hi) / 2
        half = max(hi - mid, mid - lo, 1e-9)
        devs.append(min(1.0, abs(v - mid) / half))
    return round(1 - sum(devs) / len(devs) / 2, 3)        # 1.0 on the ideal, 0.5 at the range edge


def _pt(df, label, p: Pivot) -> dict:
    return {"label": label, "idx": p.idx, "time": t(df, p.idx), "price": p.price, "confirmed_idx": p.confirmed}


def _ratios(X, A, B, C, D=None) -> dict:
    xa, ab, bc = abs(A.price - X.price), abs(B.price - A.price), abs(C.price - B.price)
    r = {"ab_xa": ab / xa if xa else None, "bc_ab": bc / ab if ab else None}
    if D is not None:
        cd = abs(D.price - C.price)
        r.update(cd_bc=cd / bc if bc else None, ad_xa=abs(D.price - A.price) / xa if xa else None)
    return {k: (round(v, 4) if v is not None else None) for k, v in r.items()}


def _prz(name: str, X, A, B, C) -> tuple[float, float] | None:
    spec = SPECS[name]
    sgn = 1 if A.price > X.price else -1                 # bullish (X low, A high): D below A
    xa, bc = abs(A.price - X.price), abs(C.price - B.price)
    lo1, hi1, _ = spec["ad_xa"]
    lo2, hi2, _ = spec["cd_bc"]
    z1 = sorted([A.price - sgn * lo1 * xa * (1 - TOL), A.price - sgn * hi1 * xa * (1 + TOL)])
    z2 = sorted([C.price - sgn * lo2 * bc * (1 - TOL), C.price - sgn * hi2 * bc * (1 + TOL)])
    lo, hi = max(z1[0], z2[0]), min(z1[1], z2[1])
    return (lo, hi) if lo <= hi else None


def detect(df: pd.DataFrame, piv: list[Pivot]) -> list[Detection]:
    n = len(df)
    out: list[Detection] = []
    lo_bar = n - LOOKBACK_BARS
    # XABCD
    for i in range(len(piv) - 3):
        X, A, B, C = piv[i:i + 4]
        D = piv[i + 4] if i + 4 < len(piv) else None
        if C.idx < lo_bar:
            continue
        base = _ratios(X, A, B, C)
        for name, spec in SPECS.items():
            if _fits(base, {k: spec[k] for k in ("ab_xa", "bc_ab")}) is None:
                continue
            prz = _prz(name, X, A, B, C)
            if prz is None:
                continue
            direction = "BUY" if A.price > X.price else "SELL"          # D is a low for a bullish pattern
            sgn = 1 if direction == "BUY" else -1
            pts = [_pt(df, lab, p) for lab, p in zip("XABC", (X, A, B, C))]
            det = Detection("harmonic", name, direction, pts, C.confirmed, "FORMING", 0.0, base,
                            {"prz": [round(prz[0], 8), round(prz[1], 8)]}, f"{VERSION}: {__doc__.splitlines()[0]}",
                            history=[{"state": "FORMING", "idx": C.confirmed, "time": t(df, C.confirmed),
                                      "reason": "X, A, B, C fit; completion zone projected"}],
                            key_points=(X.idx, A.idx, B.idx, C.idx))
            if D is None:
                # still forming: overshooting the zone (close beyond its far end) invalidates the formation
                far = prz[0] if direction == "BUY" else prz[1]
                tol = TOL * abs(A.price - X.price)
                closes = df["close"].to_numpy(float)[C.confirmed + 1:]
                over = [j for j, c in enumerate(closes) if (c - far) * sgn < -tol]
                if over:
                    k = C.confirmed + 1 + over[0]
                    det.state = "INVALIDATED"
                    det.history.append({"state": "INVALIDATED", "idx": k, "time": t(df, k),
                                        "reason": "price closed beyond the completion zone without a D pivot"})
                    det.measure = {"outcome": "INVALIDATED", "completed": False}
                det.quality = _fits(base, {k: spec[k] for k in ("ab_xa", "bc_ab")}) or 0.0
                out.append(det)
                continue
            full = _ratios(X, A, B, C, D)
            q = _fits(full, spec)
            if q is None:
                det.state = "INVALIDATED"
                det.ratios = full
                det.history.append({"state": "INVALIDATED", "idx": D.confirmed, "time": t(df, D.confirmed),
                                    "reason": "the D pivot formed outside the pattern's ratios"})
                det.measure = {"outcome": "INVALIDATED", "completed": False}
                det.quality = 0.0
                out.append(det)
                continue
            det.points.append(_pt(df, "D", D))
            det.ratios, det.quality, det.state, det.known_idx = full, q, "COMPLETED", D.confirmed
            det.history.append({"state": "COMPLETED", "idx": D.confirmed, "time": t(df, D.confirmed),
                                "reason": "D pivot confirmed inside the ratios"})
            cd = abs(D.price - C.price)
            inv = X.price if name in STOP_BEYOND_X else D.price - sgn * 0.272 * cd
            ad = A.price - D.price
            t1, t2 = D.price + 0.382 * ad, D.price + 0.618 * ad
            conf = D.price + 0.236 * ad
            det.levels.update(invalidation=round(inv, 8), targets=[round(t1, 8), round(t2, 8)],
                              confirmation=round(conf, 8))
            follow(df, det, D.confirmed, invalidation=inv, target=t1, confirmation=conf, expire_bars=EXPIRE_BARS)
            out.append(det)
    # AB=CD
    for i in range(len(piv) - 3):
        A, B, C, D = piv[i:i + 4]
        if D.idx < lo_bar:
            continue
        ab, bc, cd = abs(B.price - A.price), abs(C.price - B.price), abs(D.price - C.price)
        r = {"bc_ab": round(bc / ab, 4) if ab else None, "cd_ab": round(cd / ab, 4) if ab else None,
             "cd_bc": round(cd / bc, 4) if bc else None}
        q = _fits(r, ABCD)
        if q is None:
            continue
        direction = "BUY" if D.kind == "L" else "SELL"
        sgn = 1 if direction == "BUY" else -1
        det = Detection("harmonic", "abcd", direction, [_pt(df, lab, p) for lab, p in zip("ABCD", (A, B, C, D))],
                        D.confirmed, "COMPLETED", q, r, {}, f"{VERSION}: AB=CD (CD = AB within 10 %)",
                        history=[{"state": "COMPLETED", "idx": D.confirmed, "time": t(df, D.confirmed),
                                  "reason": "D pivot confirmed with CD equal to AB"}],
                        key_points=(A.idx, B.idx, C.idx))
        inv = D.price - sgn * 0.272 * cd
        leg = C.price - D.price
        t1, t2, conf = D.price + 0.382 * leg, D.price + 0.618 * leg, D.price + 0.236 * leg
        det.levels = {"invalidation": round(inv, 8), "targets": [round(t1, 8), round(t2, 8)],
                      "confirmation": round(conf, 8)}
        follow(df, det, D.confirmed, invalidation=inv, target=t1, confirmation=conf, expire_bars=EXPIRE_BARS)
        out.append(det)
    return out
