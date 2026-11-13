"""Market & feed anomaly detection.

Critical anomalies flag the symbol, alert the Risk Engine (new entries blocked for that symbol), keep all
existing protective stops untouched, and are recorded. They are resolved only after the condition has
been clear on ``RECOVERY_CHECKS`` consecutive checks. Nothing here closes positions.

Only broker-available data is used (bars, tick bid/ask, tick volume, spread history). Market depth /
exchange volume are reported as unavailable with source and timestamp — never fabricated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import indicators as ind
from app.db.models_intel import AnomalyEvent
from app.services.events import get_redis

UTC = timezone.utc
RECOVERY_CHECKS = 3
TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def detect(df: pd.DataFrame, tick: dict | None, timeframe: str, point: float, now: datetime | None = None,
           news_status: str = "UNKNOWN", stale_seconds: int = 120) -> list[dict]:
    now = now or datetime.now(UTC)
    out: list[dict] = []
    if len(df) < 60:
        return out
    a = ind.atr(df)
    atr = float(a.iloc[-2]) if not np.isnan(a.iloc[-2]) else float((df.high - df.low).iloc[-20:].mean())
    last, prev = df.iloc[-1], df.iloc[-2]
    rng = float(last.high - last.low)
    weekday = now.weekday() < 5 or (now.weekday() == 6 and now.hour >= 22)
    add = lambda kind, sev, text, metrics, block=False: out.append(  # noqa: E731
        {"kind": kind, "severity": sev, "explanation": text, "metrics": metrics, "blocks_entries": block})
    if tick and tick.get("time") and weekday:
        age = now.timestamp() - float(tick["time"])
        if age > stale_seconds:
            add("stale_price", "critical", f"last tick is {age:.0f}s old during market hours", {"age_s": round(age)}, True)
    if "spread" in df and df["spread"].iloc[-200:].median() > 0:
        med = float(df["spread"].iloc[-200:].median())
        cur = (float(tick["ask"]) - float(tick["bid"])) / point if tick and tick.get("ask") else float(df["spread"].iloc[-1])
        ratio = cur / med
        if ratio > 5:
            add("abnormal_spread", "critical", f"spread {cur:.0f}pt is {ratio:.1f}x its median", {"ratio": round(ratio, 2)}, True)
        elif ratio > 3:
            add("abnormal_spread", "warning", f"spread {cur:.0f}pt is {ratio:.1f}x its median", {"ratio": round(ratio, 2)})
    if atr > 0:
        r = rng / atr
        if r > 4:
            add("flash_move", "critical", f"last bar range is {r:.1f}x ATR", {"range_atr": round(r, 2)}, True)
        elif r > 2.5:
            add("volatility_spike", "warning", f"last bar range is {r:.1f}x ATR", {"range_atr": round(r, 2)})
        gap = abs(float(last.open) - float(prev.close)) / atr
        if gap > 2:
            add("price_gap", "warning", f"open gapped {gap:.1f} ATR from previous close", {"gap_atr": round(gap, 2)})
        if tick and tick.get("bid"):
            jump = abs(float(tick["bid"]) - float(last.close)) / atr
            if jump > 3:
                add("unexpected_jump", "critical", f"live price {jump:.1f} ATR away from last close", {"jump_atr": round(jump, 2)}, True)
    v = np.log1p(df["volume"].astype(float).iloc[-200:])
    if v.std() > 0:
        z = float((v.iloc[-1] - v.iloc[:-1].mean()) / v.iloc[:-1].std())
        if z > 4:
            add("tick_volume_anomaly", "warning", f"tick volume z-score {z:.1f} (broker tick volume)", {"z": round(z, 2)})
    sec = TF_SEC.get(timeframe)
    if sec:
        gaps = np.diff(df.index[-50:].asi8) / 1e9
        missing = int(((gaps > 3 * sec) & (gaps < 40 * 3600)).sum())     # exclude weekend closures
        if missing:
            add("feed_interruption", "warning", f"{missing} intra-week bar gaps in the last 50 bars", {"gaps": missing})
    if news_status == "RESTRICTED" and atr > 0 and rng / atr > 1.8:
        add("news_volatility", "warning", "high-impact news window with expanded range", {"range_atr": round(rng / atr, 2)})
    return out


def unavailable_sources(now: datetime | None = None) -> list[dict]:
    ts = (now or datetime.now(UTC)).isoformat()
    return [{"data": d, "status": "unavailable", "source": "MT5 retail feed", "checked_at": ts}
            for d in ("market_depth", "exchange_volume", "footprint", "delta")]


def record(db: Session, symbol: str, found: list[dict]) -> dict:
    """Persist new anomalies, keep active ones open, resolve after consecutive clean checks."""
    r = get_redis()
    now = datetime.now(UTC)
    active = db.scalars(select(AnomalyEvent).where(AnomalyEvent.symbol == symbol, AnomalyEvent.resolved_at.is_(None))).all()
    kinds = {f["kind"] for f in found}
    created = 0
    for f in found:
        if any(a.kind == f["kind"] for a in active):
            r.delete(f"rr:anomaly:clean:{symbol}:{f['kind']}")
            continue
        db.add(AnomalyEvent(symbol=symbol, **f))
        created += 1
    resolved = 0
    for a in active:
        if a.kind in kinds:
            continue
        n = r.incr(f"rr:anomaly:clean:{symbol}:{a.kind}")
        r.expire(f"rr:anomaly:clean:{symbol}:{a.kind}", 86400)
        if n >= RECOVERY_CHECKS:
            a.resolved_at, a.resolution = now, f"condition clear on {n} consecutive checks"
            r.delete(f"rr:anomaly:clean:{symbol}:{a.kind}")
            resolved += 1
    db.flush()
    blocking = [a for a in db.scalars(select(AnomalyEvent).where(AnomalyEvent.symbol == symbol, AnomalyEvent.resolved_at.is_(None),
                                                                 AnomalyEvent.blocks_entries.is_(True)))]
    return {"created": created, "resolved": resolved, "blocking": [a.kind for a in blocking]}


def active_block(db: Session, symbol: str, max_age: timedelta = timedelta(hours=6)) -> str | None:
    """Unresolved critical anomaly for the symbol -> reason string for the Risk Engine."""
    a = db.scalar(select(AnomalyEvent).where(AnomalyEvent.symbol == symbol, AnomalyEvent.resolved_at.is_(None),
                                             AnomalyEvent.blocks_entries.is_(True),
                                             AnomalyEvent.detected_at >= datetime.now(UTC) - max_age)
                  .order_by(AnomalyEvent.id.desc()).limit(1))
    return f"{a.kind}: {a.explanation}" if a else None
