"""DEMO validation report (Parts 27-30 Phase 9).

Phase 9 is acceptance on the owner's PC with a real DEMO account. This report is computed by the engine from what
the installation actually recorded, so that acceptance is one page instead of a manual inspection. Each item is:
* **PASS:** the evidence is there and correct;
* **ATTENTION:** evidence shows a problem to look at;
* **PENDING:** not enough activity yet to judge (for example no trades yet). Never counted as a pass.

Read-only. It cannot change the mode, the risk configuration or anything else.
"""
from __future__ import annotations

from app.services.system_check import not_test_trade  # noqa: E402

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

PASS, ATTENTION, PENDING = "PASS", "ATTENTION", "PENDING"


def report(db: Session, hours: int = 72) -> dict:
    from app.db.models import (AnalysisResult, AuditLog, ClosedPosition, OpenPosition, Symbol, TradeOrder, TradingAccount,
                               TradingSignal)
    from app.db.models_opportunity import Opportunity
    from app.services import monitoring, readiness
    from app.services import trading as tr
    from app.services.events import get_redis
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    items: list[dict] = []

    def add(key, label, status, detail, evidence=None):
        items.append({"key": key, "label": label, "status": status, "detail": detail, "evidence": evidence or {}})

    st = tr.get_state(db)
    live_switches = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "MODE_SWITCH",
                                                                    AuditLog.target == "LIVE")) or 0
    add("mode", "Running in DEMO; LIVE never switched on", PASS if st.active_mode == "DEMO" and not live_switches else ATTENTION,
        f"mode {st.active_mode}; switches to LIVE recorded: {live_switches}")
    acct = tr.active_account(db)
    try:
        health = json.loads(get_redis().get("rr:bridge:health") or "null")
    except Exception:  # noqa: BLE001
        health = None
    ok_conn = bool(acct and acct.mode == "DEMO" and acct.verified and health and health.get("connected"))
    add("broker", "DEMO account verified and MT5 connected", PASS if ok_conn and st.startup_checks_passed else
        (PENDING if acct is None else ATTENTION),
        "no active account" if acct is None else
        f"account {acct.login} ({acct.mode}), verified={acct.verified}, connected={bool(health and health.get('connected'))}, "
        f"startup reconciliation passed={st.startup_checks_passed}")
    scanned = db.scalar(select(func.count(Symbol.id)).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))) or 0
    analyses = db.scalar(select(func.count(AnalysisResult.id)).where(AnalysisResult.created_at >= since)) or 0
    add("scanning", "Symbols scanned and analysed", PASS if scanned and analyses else (PENDING if scanned else ATTENTION),
        f"{scanned} symbols enabled for scanning; {analyses} analyses in the last {hours} h")
    latest = select(AnalysisResult.symbol_id, AnalysisResult.timeframe, func.max(AnalysisResult.id).label("id")) \
        .group_by(AnalysisResult.symbol_id, AnalysisResult.timeframe).subquery()
    scores = [((a.structure or {}).get("data_quality") or {}).get("score")
              for a in db.scalars(select(AnalysisResult).join(latest, AnalysisResult.id == latest.c.id))]
    scores = [x for x in scores if isinstance(x, (int, float))]
    low = [x for x in scores if x < 70]
    add("data_quality", "Market data quality", PENDING if not scores else (ATTENTION if low else PASS),
        "no analysis with a quality score yet" if not scores else
        f"{len(scores)} symbol/timeframes, mean score {sum(scores) / len(scores):.0f}, {len(low)} below 70")
    opps = db.scalars(select(Opportunity).where(Opportunity.created_at >= since)).all()
    full = [o for o in opps if len(o.trace or []) >= 20]
    by_state: dict[str, int] = {}
    for o in opps:
        by_state[o.state] = by_state.get(o.state, 0) + 1
    add("opportunities", "Opportunities with full decision traces", PASS if full else PENDING,
        f"{len(opps)} opportunities in {hours} h ({', '.join(f'{k} {v}' for k, v in sorted(by_state.items())) or 'none'}); "
        f"{len(full)} with a full trace", {"states": by_state})
    sigs = db.scalars(select(TradingSignal).where(TradingSignal.created_at >= since,
                                                  TradingSignal.decision.in_(["BUY", "SELL"]))).all()
    reviewed = [x for x in sigs if (x.ai_review or {}).get("decision") not in (None, "UNREVIEWED")]
    errors = [x for x in sigs if (x.ai_review or {}).get("error") and "no AI provider" not in (x.ai_review or {}).get("error", "")]
    from app.ai.gateway import configured
    from app.services.trading import get_state
    if (get_state(db).ai_mode or "AI") == "ENGINE_ONLY":
        skipped = [x for x in sigs if (x.ai_review or {}).get("decision") == "SKIPPED"]
        add("ai_review", "AI review of candidates", PASS,
            f"engine-only mode (Run without AI): AI review switched off by the operator; {len(skipped)} of {len(sigs)} "
            f"BUY/SELL candidates went to the Risk Engine without AI review")
    elif not configured("candidate_review"):
        add("ai_review", "AI review of candidates", PENDING,
            "no AI provider configured: candidates stay unreviewed and are never auto-executed (configure one in AI Studio)")
    else:
        add("ai_review", "AI review of candidates", PASS if reviewed and not errors else (ATTENTION if errors else PENDING),
            f"{len(reviewed)} of {len(sigs)} BUY/SELL candidates reviewed; {len(errors)} review errors")
    demo_ids = [a.id for a in db.scalars(select(TradingAccount).where(TradingAccount.mode == "DEMO"))]
    orders = db.scalars(select(TradeOrder).where(TradeOrder.account_id.in_(demo_ids or [-1]), TradeOrder.created_at >= since,
                                                 TradeOrder.signal_id.isnot(None))).all() if demo_ids else []
    confirmed = [o for o in orders if o.status == "CONFIRMED"]
    per_signal: dict[int, int] = {}
    for o in confirmed:
        if o.action.startswith("MARKET_") or o.action.endswith(("_LIMIT", "_STOP")):
            per_signal[o.signal_id] = per_signal.get(o.signal_id, 0) + 1
    dup = [k for k, v in per_signal.items() if v > 1]
    add("execution", "DEMO orders executed once each", PENDING if not confirmed else (ATTENTION if dup else PASS),
        f"{len(confirmed)} confirmed entries from signals in {hours} h; signals with more than one entry: {len(dup)}")
    unprotected = db.scalars(select(OpenPosition).where(OpenPosition.stop_loss.is_(None))).all()
    open_n = db.scalar(select(func.count(OpenPosition.id))) or 0
    add("protection", "Every open position has a stop", ATTENTION if unprotected else (PASS if open_n else PENDING),
        f"{open_n} open, {len(unprotected)} without a stop")
    closed = db.execute(select(func.count(ClosedPosition.id), func.coalesce(func.sum(ClosedPosition.profit), 0.0))
                        .where(ClosedPosition.account_id.in_(demo_ids or [-1]), not_test_trade())).one()
    rd = readiness.report(db)
    demo_item = next(i for i in rd["items"] if i["key"] == "demo_acceptance")
    add("demo_record", "DEMO track record", PASS if demo_item["passed"] and float(closed[1]) >= 0 else
        (ATTENTION if demo_item["passed"] else PENDING),
        f"{int(closed[0])} closed DEMO trades, net {float(closed[1]):.2f}"
        + (" (net negative: review the results before considering LIVE)" if demo_item["passed"] and float(closed[1]) < 0 else ""))
    snap = monitoring.snapshot(db)
    bl = snap["backlog"]
    errs = sum(snap["log_errors_15m"].values())
    api = snap["api"]
    add("operations", "Background jobs and errors", ATTENTION if bl["learner_stuck"] or errs or (api["server_errors"] or 0)
        else PASS,
        f"stuck jobs {len(bl['learner_stuck'])}, overdue {len(bl['learner_overdue'])}, error logs (15 min) {errs}, "
        f"API server errors (15 min) {api['server_errors']}")
    add("backups", "Automatic backups and restore tests", PENDING,
        "shown in the desktop window (Backup and Recovery): the engine cannot see the PC's backups folder")
    counts = {s: sum(1 for i in items if i["status"] == s) for s in (PASS, ATTENTION, PENDING)}
    return {"generated_at": now.isoformat(), "window_hours": hours, "items": items, "counts": counts,
            "accepted": counts[ATTENTION] == 0 and counts[PENDING] <= 1,
            "note": "PENDING is never a pass. Acceptance needs every item PASS except the backups, which are checked in "
                    "the desktop window."}
