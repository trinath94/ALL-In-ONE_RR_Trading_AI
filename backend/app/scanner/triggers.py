"""Event-driven re-analysis (Parts 27-30 Phase 4).

The scanner normally analyses a symbol/timeframe once per closed bar. Three events make a job due again before the
next bar closes:

* ``event:news_release``: a high-impact calendar event for one of the symbol's currencies has just published its
  actual value;
* ``event:anomaly_<kind>``: the anomaly detector recorded a flash move, volatility spike, price gap or jump;
* ``event:price_at_entry``: the price has moved into ``ENTRY_ZONE_ATR`` ATR of a WAITING_FOR_CONFIRMATION
  opportunity's entry since that analysis (that timeframe only).

News and anomalies re-analyse only the timeframes whose latest analysis predates the event: an analysis made after
it already took it into account.

Guards against storms and duplicates: one request per symbol per ``reanalysis_throttle_seconds``; each news event and
anomaly fires once; the scanner's per-job in-flight lock still prevents concurrent analyses of one job; and a
re-analysis of an already analysed bar supersedes that bar's still-NEW signal instead of adding a second one
(see ``app.scanner.service.scan_once``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("scanner.triggers")
ENTRY_ZONE_ATR = 0.25
ANOMALY_KINDS = ("flash_move", "volatility_spike", "price_gap", "unexpected_jump")


def reason_key(symbol_id: int, tf: str) -> str:
    return f"rr:reanalyse:{symbol_id}:{tf}"


def request(rds, symbol_id: int, timeframes: list[str], reason: str, throttle_s: int) -> bool:
    """Make these jobs due. False when this symbol was already re-requested within the throttle period."""
    if not rds.set(f"rr:reanalyse:throttle:{symbol_id}", reason, nx=True, ex=max(1, int(throttle_s))):
        return False
    for tf in timeframes:
        rds.set(reason_key(symbol_id, tf), reason, ex=3600)
    return True


def pending(rds, symbol_id: int, tf: str) -> str | None:
    v = rds.get(reason_key(symbol_id, tf))
    return v.decode() if isinstance(v, bytes) else v


def clear(rds, symbol_id: int, tf: str) -> None:
    rds.delete(reason_key(symbol_id, tf))


def stale_timeframes(db: Session, symbol_id: int, timeframes: list[str], since: datetime) -> list[str]:
    """The timeframes whose latest analysis is older than ``since`` (an analysis made after the event already saw
    it, so re-analysing would only repeat it)."""
    from sqlalchemy import func

    from app.db.models import AnalysisResult
    latest = dict(db.execute(select(AnalysisResult.timeframe, func.max(AnalysisResult.created_at))
                             .where(AnalysisResult.symbol_id == symbol_id, AnalysisResult.timeframe.in_(timeframes))
                             .group_by(AnalysisResult.timeframe)).all())
    return [tf for tf in timeframes if latest.get(tf) is None or latest[tf] < since]


def check(db: Session, bridge, rds, timeframes: list[str], now: datetime, throttle_s: int) -> list[dict]:
    """Look for the three events; request re-analysis where one occurred. Returns what was requested."""
    from app.db.models import EconomicEvent, Symbol
    from app.db.models_intel import AnomalyEvent
    from app.news.service import is_high_impact, symbol_currencies
    out: list[dict] = []
    symbols = db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))).all()

    # 1. News releases: an actual value published in the last 30 minutes.
    for ev in db.scalars(select(EconomicEvent).where(EconomicEvent.actual.is_not(None),
                                                     EconomicEvent.scheduled_at.between(now - timedelta(minutes=30), now))):
        if not is_high_impact(ev) or not rds.set(f"rr:reanalyse:seen:news:{ev.id}", "1", nx=True, ex=86400):
            continue
        for sym in symbols:
            if ev.currency not in symbol_currencies(sym, sym.broker_symbol):
                continue
            tfs = stale_timeframes(db, sym.id, timeframes, ev.fetched_at or ev.scheduled_at)
            if tfs and request(rds, sym.id, tfs, "event:news_release", throttle_s):
                out.append({"symbol": sym.broker_symbol, "reason": "event:news_release", "event": ev.title,
                            "timeframes": tfs})

    # 2. Anomalies detected in the last 10 minutes.
    by_name = {s.broker_symbol: s for s in symbols}
    for a in db.scalars(select(AnomalyEvent).where(AnomalyEvent.detected_at >= now - timedelta(minutes=10),
                                                   AnomalyEvent.kind.in_(ANOMALY_KINDS))):
        sym = by_name.get(a.symbol)
        if sym is None or not rds.set(f"rr:reanalyse:seen:anomaly:{a.id}", "1", nx=True, ex=86400):
            continue
        reason = f"event:anomaly_{a.kind}"
        tfs = stale_timeframes(db, sym.id, timeframes, a.detected_at)
        if tfs and request(rds, sym.id, tfs, reason, throttle_s):
            out.append({"symbol": sym.broker_symbol, "reason": reason, "timeframes": tfs})

    # 3. Price at the entry of an opportunity waiting for confirmation (created in the last day).
    from app.db.models import AnalysisResult, TradingSignal
    from app.db.models_opportunity import Opportunity
    rows = db.execute(select(Opportunity, TradingSignal, AnalysisResult)
                      .join(TradingSignal, TradingSignal.id == Opportunity.signal_id)
                      .join(AnalysisResult, AnalysisResult.id == Opportunity.analysis_id)
                      .where(Opportunity.state == "WAITING_FOR_CONFIRMATION", TradingSignal.status == "NEW",
                             Opportunity.created_at >= now - timedelta(days=1))
                      .order_by(Opportunity.id.desc()).limit(50)).all()
    ids = {s.id: s for s in symbols}
    for opp, sig, ar in rows:
        sym = ids.get(opp.symbol_id)
        atr = (ar.structure or {}).get("atr")
        if sym is None or not atr or sig.entry is None or opp.timeframe not in timeframes:
            continue
        try:
            tick = bridge.tick(sym.broker_symbol) or {}
        except Exception:  # noqa: BLE001 - a missing quote only means no trigger
            continue
        px = float(tick.get("bid") or 0)
        zone = ENTRY_ZONE_ATR * float(atr)
        then = (ar.structure or {}).get("price")
        # Only a move *into* the zone since the analysis: if the price was already there, the analysis saw it.
        if then is None or abs(float(then) - float(sig.entry)) <= zone:
            continue
        if px and abs(px - float(sig.entry)) <= zone and \
                request(rds, sym.id, [opp.timeframe], "event:price_at_entry", throttle_s):
            out.append({"symbol": sym.broker_symbol, "reason": "event:price_at_entry", "opportunity": opp.id})
    if out:
        log.info("event-driven re-analysis requested: %s", out)
    return out
