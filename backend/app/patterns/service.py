"""Runs the pattern detectors inside an analysis and keeps their records (Part 28.3-28.6).

* **Deduplication:** a formation's key is symbol + timeframe + pattern + the *times* of its defining pivots, so the
  next analysis updates the same record.
* **Forward-only:** a record only moves forward through its lifecycle. A closed record (target reached, invalidated,
  expired) is final.
* **Revised structure:** an open record whose formation is no longer found becomes INVALIDATED ("pivots revised").
  This happens when a later, more extreme pivot replaces one of its pivots. It is never left open silently.
* **Relations (28.5):** the original strategies that fired on the same bar in the same direction (supporting) or the
  opposite one (contradicting), recorded while the formation is open.
* **Learning (28.6):** outcome statistics per pattern, timeframe and regime, from the measurements on price bars, with
  sample sizes; fewer than 30 closed formations is flagged as insufficient.
* **Advisory:** nothing here changes a strategy signal or a trading decision.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_opportunity import PatternDetection
from app.patterns import elliott, harmonic
from app.patterns.lifecycle import CLOSED, OPEN, STATES
from app.patterns.pivots import zigzag

log = logging.getLogger("patterns")
RANK = {s: i for i, s in enumerate(STATES)}
LIVE_BARS = 2              # known within the last bars of the analysis: found live, not reconstructed from history
MIN_SAMPLE = 30


def _key(sym_id: int, tf: str, pattern: str, df: pd.DataFrame, idxs: tuple) -> str:
    return f"{sym_id}:{tf}:{pattern}:" + "-".join(str(int(df.index[i].timestamp())) for i in idxs)


def run(db: Session, sym, tf: str, df: pd.DataFrame, *, regime: str, sessions: list[str], spread_points: float | None,
        sigs: list, data_caps: list[str]) -> dict:
    piv = zigzag(df)
    dets = harmonic.detect(df, piv) + elliott.detect(df, piv)
    last = len(df) - 1
    bar = df.index[-1].to_pydatetime()
    seen, new, updated = set(), 0, 0
    active = []
    for d in dets:
        key = _key(sym.id, tf, d.pattern, df, d.key_points)
        seen.add(key)
        support = sorted({s.strategy for s in sigs if s.direction == d.direction})
        contra = sorted({s.strategy for s in sigs if s.direction != d.direction})
        row = db.scalar(select(PatternDetection).where(PatternDetection.key == key))
        payload = {"state": d.state, "quality": float(d.quality), "points": d.points, "ratios": d.ratios,
                   "levels": d.levels, "history": d.history, "measure": d.measure, "direction": d.direction}
        if row is None:
            row = PatternDetection(key=key, symbol_id=sym.id, timeframe=tf, family=d.family, pattern=d.pattern,
                                   rules=d.rules, detector_version=d.rules.split(":")[0],
                                   known_at=df.index[d.known_idx].to_pydatetime(),
                                   found_live=d.known_idx >= last - LIVE_BARS,
                                   context={"regime": regime, "sessions": sessions, "spread_points": spread_points,
                                            "data_caps": data_caps, "bars": len(df), "analysed_bar": bar.isoformat()},
                                   relations={}, created_at=datetime.now(UTC), updated_at=datetime.now(UTC), **payload)
            db.add(row)
            new += 1
        elif row.state not in CLOSED and (RANK[d.state] > RANK[row.state] or d.state in CLOSED or
                                          d.measure != row.measure):
            for k, v in payload.items():
                setattr(row, k, v)
            if d.known_idx < len(df):
                row.known_at = df.index[d.known_idx].to_pydatetime()
            row.updated_at = datetime.now(UTC)
            updated += 1
        if row.state in OPEN:
            row.relations = {"bar": bar.isoformat(), "supporting": support, "contradicting": contra}
            active.append(row)
    # open records of this symbol/timeframe whose formation is no longer found within the window: structure revised
    first = df.index[0].to_pydatetime()
    for row in db.scalars(select(PatternDetection).where(PatternDetection.symbol_id == sym.id,
                                                         PatternDetection.timeframe == tf,
                                                         PatternDetection.state.in_(OPEN))):
        if row.key in seen:
            continue
        if row.known_at < first:
            reason, state = "the formation left the analysed window while still open", "EXPIRED"
        else:
            reason, state = "pivots revised: the formation is no longer present", "INVALIDATED"
        row.history = [*row.history, {"state": state, "time": bar.isoformat(), "reason": reason}]
        row.state, row.updated_at = state, datetime.now(UTC)
        row.measure = {**(row.measure or {}), "outcome": state}
        updated += 1
    db.flush()
    return {"pivots": len(piv), "detections": len(dets), "new": new, "updated": updated,
            "active": [{"id": r.id, "pattern": r.pattern, "direction": r.direction, "state": r.state,
                        "quality": r.quality, "supporting": r.relations.get("supporting", []),
                        "contradicting": r.relations.get("contradicting", [])} for r in active]}


def relation_to(active: list[dict], direction: str | None) -> dict:
    """How open formations relate to the engine's direction (for the decision trace)."""
    if not direction:
        return {"agree": [], "disagree": []}
    return {"agree": [a["pattern"] for a in active if a["direction"] == direction],
            "disagree": [a["pattern"] for a in active if a["direction"] != direction]}


def stats(db: Session, days: int = 90, live_only: bool = False) -> list[dict]:
    """Outcomes of closed formations that completed (pattern learning, Part 28.6)."""
    q = select(PatternDetection).where(PatternDetection.state.in_(CLOSED),
                                       PatternDetection.created_at >= datetime.now(UTC) - timedelta(days=days))
    if live_only:
        q = q.where(PatternDetection.found_live.is_(True))
    groups: dict[tuple, list[PatternDetection]] = defaultdict(list)
    for r in db.scalars(q):
        groups[(r.pattern, r.timeframe, (r.context or {}).get("regime") or "?")].append(r)
    out = []
    for (pattern, tf, regime), rows in sorted(groups.items()):
        measured = [r for r in rows if (r.measure or {}).get("r")]
        hit = sum(1 for r in measured if r.state == "TARGET_REACHED")
        fail = sum(1 for r in measured if r.state == "INVALIDATED")
        false_break = sum(1 for r in measured if r.state == "INVALIDATED" and
                          any(h.get("state") == "CONFIRMED" for h in r.history or []))
        never = sum(1 for r in rows if not (r.measure or {}).get("r"))
        decided = hit + fail
        out.append({"pattern": pattern, "timeframe": tf, "regime": regime, "closed": len(rows),
                    "measured": len(measured), "target_reached": hit, "invalidated": fail, "false_breakouts": false_break,
                    "expired": sum(1 for r in measured if r.state == "EXPIRED"), "did_not_complete": never,
                    "success_rate": round(hit / decided, 4) if decided else None,
                    "avg_mfe_r": round(sum(r.measure["mfe_r"] for r in measured) / len(measured), 3) if measured else None,
                    "avg_mae_r": round(sum(r.measure["mae_r"] for r in measured) / len(measured), 3) if measured else None,
                    "sufficient": decided >= MIN_SAMPLE})
    return out


def alignment(db: Session, symbol_id: int, timeframes: list[str]) -> dict:
    """Part 28.4: each configured timeframe's latest regime direction, and whether they agree."""
    from app.db.models import AnalysisResult
    from app.signals.engine import TF_MINUTES
    per = {}
    for tf in sorted(timeframes, key=lambda x: TF_MINUTES.get(x, 0)):
        a = db.scalar(select(AnalysisResult).where(AnalysisResult.symbol_id == symbol_id, AnalysisResult.timeframe == tf)
                      .order_by(AnalysisResult.id.desc()).limit(1))
        if a is None:
            per[tf] = {"direction": None, "regime": None, "bar_time": None}
            continue
        reg = a.regime or ""
        d = 1 if reg.endswith("BULL") else (-1 if reg.endswith("BEAR") else 0)
        per[tf] = {"direction": d, "regime": reg, "bar_time": a.bar_time.isoformat()}
    known = [v["direction"] for v in per.values() if v["direction"] is not None]
    nonzero = [x for x in known if x]
    return {"timeframes": per, "known": len(known),
            "aligned": bool(nonzero) and len(set(nonzero)) == 1 and len(nonzero) == len(known),
            "bias": (1 if sum(nonzero) > 0 else -1 if sum(nonzero) < 0 else 0) if nonzero else 0,
            "basis": "each timeframe's latest analysed regime (BULL/BEAR = +1/-1, others 0)"}
