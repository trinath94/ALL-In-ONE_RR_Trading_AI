"""Market data quality (Parts 27-30 Phase 7, Part 30.3).

Checks on the raw bars the broker returned, before any strategy sees them:
* duplicate and out-of-order candle times;
* invalid prices: a high below the low, an open or close outside the range, or a non-positive price;
* unexplained gaps: missing bars on a weekday, outside the Friday 21:00 to Sunday 23:00 UTC weekend;
* the broker server-time offset: the age of the quote and of the last closed bar against the engine's clock.

The 0-100 score starts at 100 and loses points per problem found. Each penalty is stated and nothing is estimated.
The source is identified (MT5 server, symbol, timeframe). The score is recorded with each analysis and its decision
trace. It does not change a decision: stale quotes and feed interruptions are already blocked by the anomaly rules.
"""
from __future__ import annotations

from datetime import UTC, datetime

PENALTY = {"duplicate": 10, "out_of_order": 10, "invalid_price": 10, "gap": 2, "stale_quote": 20, "stale_bars": 20}
MAX_GAP_PENALTY = 30


def _weekend(a: datetime, b: datetime) -> bool:
    """True if the interval touches the FX weekend (Friday 21:00 UTC to Sunday 23:00 UTC)."""
    t = a
    while t < b:
        wd, h = t.weekday(), t.hour
        if (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 23):
            return True
        t = t.replace(minute=0, second=0, microsecond=0)
        t = datetime.fromtimestamp(t.timestamp() + 3600, UTC)
    return False


def assess(rows: list[dict], tf_minutes: int, *, now: datetime | None = None, tick: dict | None = None,
           source: str = "", max_quote_age_s: int = 120) -> dict:
    now = now or datetime.now(UTC)
    times = [int(r["time"]) for r in rows if "time" in r]
    problems: dict[str, int] = {}
    dup = len(times) - len(set(times))
    order = sum(1 for a, b in zip(times, times[1:]) if b < a)
    bad = 0
    for r in rows:
        o, h, lo, c = (float(r.get(k, 0)) for k in ("open", "high", "low", "close"))
        if h < lo or not (lo <= o <= h) or not (lo <= c <= h) or min(o, h, lo, c) <= 0:
            bad += 1
    step = tf_minutes * 60
    gaps = []
    srt = sorted(set(times))
    for a, b in zip(srt, srt[1:]):
        if b - a > 1.5 * step:
            da, db = datetime.fromtimestamp(a, UTC), datetime.fromtimestamp(b, UTC)
            if not _weekend(da, db) and tf_minutes < 1440:
                gaps.append({"from": da.isoformat(), "to": db.isoformat(), "missing_bars": int((b - a) // step) - 1})
    problems.update({k: v for k, v in (("duplicate", dup), ("out_of_order", order), ("invalid_price", bad),
                                        ("gap", len(gaps))) if v})
    quote_age = None
    if tick and tick.get("time"):
        quote_age = round(now.timestamp() - float(tick["time"]), 1)
        if quote_age > max_quote_age_s:
            problems["stale_quote"] = 1
    last_close = (srt[-1] + step) if srt else None
    bars_age = round(now.timestamp() - last_close, 1) if last_close else None
    if bars_age is not None and bars_age > 3 * step and not _weekend(datetime.fromtimestamp(last_close, UTC), now):
        problems["stale_bars"] = 1
    score = 100
    for k, n in problems.items():
        score -= min(PENALTY[k] * n, MAX_GAP_PENALTY) if k == "gap" else PENALTY[k] * min(n, 3)
    return {"score": max(0, score), "source": source, "bars": len(rows), "problems": problems,
            "gaps": gaps[:10], "quote_age_s": quote_age, "last_bar_age_s": bars_age,
            "server_time_offset_s": quote_age,
            "basis": "100 minus stated penalties per problem; the quote age is the broker clock against the engine's"}
