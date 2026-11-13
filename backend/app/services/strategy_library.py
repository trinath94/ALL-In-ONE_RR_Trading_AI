"""Strategy library view (RR Trading One Phase 7).

Read-only facts about the 215 production strategies, for the console:

* what data each strategy needs and whether the engine can actually supply it today ("data status"), from the
  strategy's own ``requires`` and from what the scanner really passes to strategies (``scanner.service.analyse``);
* whether the scanner runs it in DEMO and in LIVE, using the scanner's own selection (``enabled_strategies``);
* whether its identity, parameters and source still match the frozen baseline inventory;
* what it actually did in recent analyses (signals, errors), counted from stored analysis results only.

Nothing here changes a strategy or its registry entry, and nothing is estimated: when a fact is not known the value
says so.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.scanner.service import enabled_strategies
from app.strategies import registry

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]
ACTIVITY_DAYS = 7

# Status order, best first. A strategy takes the worst status among its requirements.
STATUS_ORDER = ["runnable", "per_symbol", "not_supplied", "unsupported"]
STATUS_LABEL = {
    "runnable": "Runnable on standard MT5 data",
    "per_symbol": "Runs only on symbols whose feed provides the data",
    "not_supplied": "Not runnable yet: the engine does not supply the data",
    "unsupported": "Not runnable: MetaTrader 5 does not provide the data",
}
# What the scanner gives strategies today (scanner.service.analyse): tick volume always, real volume when the feed
# reports it, peer bars when the symbol has configured peers (Phase 8). Every other capability is never set, so
# strategies needing it never run.
CAPABILITIES = {
    "tick_volume": ("runnable", "tick volume: provided by MT5 for every symbol"),
    "real_volume": ("per_symbol", "real (exchange) volume: only where the broker feed reports it"),
    "peer_symbols": ("per_symbol", "peer-symbol data: supplied for symbols with configured peers the broker offers "
                                   "(Broker Symbols page)"),
    "news_feed": ("not_supplied", "news feed: the engine has a news service, but the scanner does not pass it to "
                                  "strategies"),
    "trained_model": ("not_supplied", "trained model: no validated model artefact is passed to strategies"),
    "claude_agent": ("not_supplied", "AI agent: agents are not called from inside strategies"),
    "portfolio_allocator": ("not_supplied", "portfolio allocator: none is connected to strategies"),
    "order_flow": ("unsupported", "order flow (bid/ask aggressor trades): not available from MT5 retail feeds"),
    "market_depth": ("unsupported", "market depth (level 2): not available to strategies from MT5 retail feeds"),
    "open_interest": ("unsupported", "open interest: not available from MT5 retail feeds"),
    "options_implied_vol": ("unsupported", "options implied volatility: not available from MT5"),
    "hft_infrastructure": ("unsupported", "co-located low-latency execution: not available on a desktop install"),
}
BASELINE_FIELDS = ("name", "category", "component", "params", "timeframes", "regimes", "requires", "risky",
                   "code_hash")


def data_status(requires: list[str]) -> tuple[str, list[str]]:
    statuses, notes = ["runnable"], []
    for cap in requires:
        status, note = CAPABILITIES.get(cap, ("unsupported", f"{cap}: unknown requirement"))
        statuses.append(status)
        notes.append(note)
    return max(statuses, key=STATUS_ORDER.index), notes or ["OHLC bars only"]


def baseline_path() -> Path:
    configured = get_settings().strategy_baseline_path
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "tests" / "baseline" / "strategy_inventory.json"


@lru_cache
def _baseline(path: str, mtime: float) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def baseline() -> dict | None:
    p = baseline_path()
    try:
        return _baseline(str(p), os.path.getmtime(p))
    except OSError:
        return None


def baseline_status() -> dict[str, str] | None:
    """code -> unchanged | modified | not_in_baseline; None when the baseline inventory is not shipped."""
    base = baseline()
    if base is None:
        return None
    out = {}
    for m in registry.metas():
        b = base["strategies"].get(m.code)
        if b is None:
            out[m.code] = "not_in_baseline"
            continue
        cur = json.loads(json.dumps({k: getattr(m, k) for k in BASELINE_FIELDS}, default=str))
        out[m.code] = "unchanged" if cur == b else "modified"
    return out


def effective(db: Session) -> dict[str, set[str]]:
    return {mode: set(enabled_strategies(db, mode)) for mode in ("DEMO", "LIVE")}


def activity(db: Session, days: int = ACTIVITY_DAYS) -> dict:
    """Signals and errors per strategy from stored analysis results (the scanner stores every signal and every
    strategy error; 'no signal' and 'unavailable' outcomes are not stored, so they cannot be counted)."""
    since = datetime.now(UTC) - timedelta(days=days)
    analyses = db.execute(text("select count(*) from analysis_results where created_at >= :s"), {"s": since}).scalar()
    rows = db.execute(text(
        "select o->>'strategy' as code, "
        "       count(*) filter (where o->>'status' = 'error') as errors, "
        "       count(*) filter (where o ? 'direction' and coalesce(o->>'status', '') <> 'error') as signals, "
        "       max(a.created_at) filter (where o ? 'direction' and coalesce(o->>'status', '') <> 'error') as last_signal "
        "from analysis_results a, jsonb_array_elements(a.strategy_outputs) o "
        "where a.created_at >= :s and jsonb_typeof(a.strategy_outputs) = 'array' "
        "group by 1"), {"s": since}).all()
    per = {r.code: {"signals": r.signals, "errors": r.errors, "last_signal_at": r.last_signal} for r in rows if r.code}
    return {"days": days, "analyses": analyses, "per_strategy": per}


OUTCOMES = ("signal", "no_signal", "unavailable", "error", "not_applicable")


def evaluations(days: int = ACTIVITY_DAYS) -> dict[str, dict[str, int]]:
    """Per-strategy outcome counts over the last `days` days, from the scanner's per-day counters (Phase 8:
    scanner.service.record_evaluations). Days before Phase 8 or after a Redis reset simply count nothing."""
    from app.scanner.service import EVAL_KEY
    from app.services.events import get_redis
    out: dict[str, dict[str, int]] = {}
    try:
        rds = get_redis()
        today = datetime.now(UTC)
        for i in range(days):
            for field, n in rds.hgetall(EVAL_KEY.format(day=(today - timedelta(days=i)).strftime("%Y%m%d"))).items():
                field = field.decode() if isinstance(field, bytes) else field
                code, _, outcome = field.rpartition("|")
                if outcome in OUTCOMES:
                    d = out.setdefault(code, dict.fromkeys(OUTCOMES, 0))
                    d[outcome] += int(n)
    except Exception:  # noqa: BLE001 - statistics are best effort
        return {}
    return out


def summary(db: Session) -> dict:
    metas = registry.metas()
    eff = effective(db)
    base = baseline_status()
    statuses = Counter(data_status(m.requires)[0] for m in metas)
    tf = {t: sum(t in m.timeframes for m in metas) for t in TIMEFRAMES}
    cats = Counter(m.category for m in metas)
    act = activity(db)
    ev = evaluations()
    return {
        "total": len(metas),
        "expected_total": sum(registry.EXPECTED_COUNTS.values()),
        "categories": [{"category": c, "count": cats.get(c, 0), "expected": n}
                       for c, n in registry.EXPECTED_COUNTS.items()],
        "baseline": None if base is None else {
            "unchanged": sum(v == "unchanged" for v in base.values()),
            "modified": sorted(k for k, v in base.items() if v == "modified"),
            "not_in_baseline": sorted(k for k, v in base.items() if v == "not_in_baseline"),
            "baseline_count": len(baseline()["strategies"]),
            "source_commit": baseline().get("source_commit"),
        },
        "data_status": [{"status": s, "label": STATUS_LABEL[s], "count": statuses.get(s, 0)} for s in STATUS_ORDER],
        "risky": sorted(m.code for m in metas if m.risky),
        "scanned": {mode: len(codes) for mode, codes in eff.items()},
        "timeframes": tf,
        "timeframes_enforced": get_settings().scan_enforce_timeframes,
        "timeframes_note": ("The scanner runs each strategy only on the timeframes it declares; on other timeframes it "
                            "is counted as not applicable." if get_settings().scan_enforce_timeframes else
                            "Timeframe enforcement is turned off (RR_SCAN_ENFORCE_TIMEFRAMES=false): every enabled "
                            "strategy runs on every scanned timeframe."),
        "activity": {"days": act["days"], "analyses": act["analyses"],
                     "strategies_with_signals": sum(1 for v in act["per_strategy"].values() if v["signals"]),
                     "strategies_with_errors": sorted(k for k, v in act["per_strategy"].items() if v["errors"])},
        "evaluations": {o: sum(v[o] for v in ev.values()) for o in OUTCOMES},
    }
