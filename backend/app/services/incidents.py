"""Error detection, explanation, diagnosis and safe self-healing (master upgrade, section 12).

DETECT -> EXPLAIN -> DIAGNOSE -> RECOMMEND FIX -> SAFELY REPAIR -> VERIFY -> RECORD -> LEARN, on the existing
monitoring: every warning/error/critical ``system_event`` (connector, execution, worker, risk, monitor, ...), unhandled
API exceptions, console (browser) errors, and periodic detectors (MT5 bridge health, stuck learner jobs, resources and
disk, AI providers cooling down, data sources, API 5xx rate). Nothing here creates a second monitoring system: it
classifies and de-duplicates what the existing components already report, and adds the missing detectors.

Safety of repairs. The catalogue holds only actions that cannot change trading: no order, position, kill switch, risk
lock, account or LIVE change, no restart of the trading worker, no AI-generated action. Each repair is verified
afterwards and every attempt is recorded; three failed attempts escalate to a critical notification. Automatic repair
can be switched off by an administrator (automation policy).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models_opportunity import Incident

log = logging.getLogger("incidents")
UTC = timezone.utc
SEVERITY = {"info": 0, "warning": 1, "error": 2, "critical": 3}
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=5)
POPUP_MIN_SEVERITY = "warning"


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    severity: str
    explanation: str
    diagnosis: str
    fix: str
    repair: str | None = None
    patterns: tuple = ()             # (component regex or "", message regex)


C = Category
CATEGORIES: list[Category] = [
    C("identity_mismatch", "MT5 account identity mismatch", "critical",
      "The MT5 terminal is logged in to a different account or server than the one configured.",
      "Execution was stopped (kill switch) so no order reaches the wrong account.",
      "Log the terminal in to the configured account (MT5 Manager), check the profile, then reset the kill switch.",
      None, (("", r"identity|wrong account|login .*does not match"),)),
    C("unknown_pending_orders", "Unknown engine orders at the broker", "critical",
      "The broker holds engine-tagged pending orders that the execution journal does not know.",
      "New entries are blocked until an operator resolves them.",
      "Compare the broker's pending orders with the journal (Trading Console), cancel or adopt them, then restart the "
      "worker's reconciliation.", None, (("", r"unknown engine pending orders"),)),
    C("kill_switch", "Kill switch active", "critical",
      "Trading was stopped by the kill switch.", "New entries are blocked on every account.",
      "Review the reason; an administrator resets the kill switch in the Risk Centre.", None,
      (("execution", r"^kill switch"),)),
    C("risk_lock", "Risk limit reached", "critical",
      "A risk limit (daily/weekly loss, drawdown, ...) locked new entries.", "The Risk Engine works as designed.",
      "Review the limit in the Risk Centre; it is reset by an administrator, never automatically.", None,
      (("risk", r"risk lock"),)),
    C("execution_unknown", "Order outcome unknown", "error",
      "An order was sent but its outcome is not known yet (network or terminal interruption).",
      "The execution journal must be reconciled with the connector; the order is never resubmitted.",
      "Automatic: reconcile the journal through the connector.", "reconcile_journal",
      (("execution", r"outcome unknown|reconciliation required"),)),
    C("order_rejected", "Broker rejected an order", "warning",
      "The broker refused an order (retcode).", "Typical causes: market closed, invalid volume or stops, no money.",
      "Check the rejection detail; the Risk Engine and symbol specification limit volume and stops.", None,
      (("", r"rejected by broker|retcode|order rejected"),)),
    C("mt5_disconnected", "MT5 terminal or connector disconnected", "error",
      "The engine cannot reach the MT5 terminal through the connector.",
      "New entries stop; each account is reconciled again after reconnection.",
      "Check that MetaTrader 5 is running and logged in, and that the RR MT5 Connector is running (MT5 Manager).",
      "verify_mt5", (("", r"disconnect|not connected|bridge unavailable|connector offline|mt5 .*(down|lost)"),)),
    C("invalid_symbol", "Invalid or unknown symbol", "warning",
      "A broker symbol is unknown or no longer offered.", "The symbol catalogue may be out of date.",
      "Automatic: refresh the broker symbol catalogue.", "refresh_symbols",
      (("", r"unknown broker symbol|invalid symbol|symbol .*not (found|offered)"),)),
    C("worker_stalled", "Background job stalled or overdue", "warning",
      "A learner job is running far beyond its timeout or is overdue.", "The learner process may have stopped mid-job.",
      "Automatic: mark the stuck job interrupted so it is retried.", "recover_stale_jobs",
      (("monitor", r"job backlog|stuck|overdue"),)),
    C("database", "Database problem", "critical",
      "The database could not be reached or a query failed.", "PostgreSQL may be down, full or restarting.",
      "Check the postgres service (System Health) and the disk space.", None,
      (("", r"psycopg|operationalerror|database|could not connect to server"),)),
    C("redis", "Redis problem", "error", "Redis could not be reached.", "Live events and locks are degraded.",
      "Check the redis service (System Health).", None, (("", r"redis"),)),
    C("ai_provider", "AI provider failing", "warning",
      "An AI provider keeps failing or rate-limits requests.", "AI review pauses for that provider (cool-down).",
      "Check the provider's key, model and billing in AI Studio, or switch the active provider.", None,
      (("ai", r""),)),
    C("ai_budget", "AI budget alert", "warning", "AI spend has passed the alert threshold of a daily or monthly "
      "budget (or the daily token limit).", "When the limit itself is reached, AI calls stop and the engine continues "
      "without AI according to the unavailable-AI policy.", "Raise the budget or lower AI usage in AI Studio → Budget; "
      "nothing else is needed.", None, (("ai", r"budget alert"),)),
    C("auth_failure", "Authentication failures", "warning", "Repeated failed logins or rejected credentials.",
      "Someone may be using a wrong password, or a credential was rotated.",
      "Check the users list and the audit log; rotate credentials if unexpected.", None,
      (("", r"login failed|authentication|unauthori[sz]ed|bad signature"),)),
    C("configuration", "Missing or invalid configuration", "error", "A required setting is missing or invalid.",
      "The affected function cannot start.", "Check the setting named in the message (Settings, AI Studio, desktop).",
      None, (("", r"not configured|missing .*(setting|config)|invalid .*(setting|config)"),)),
    C("resources_high", "High resource usage", "warning", "A service is close to its memory or CPU limit.",
      "Sustained pressure slows scans and can stop a service.",
      "Reduce the scanned symbols/timeframes or raise the limits (Performance settings).", None,
      (("resources", r""),)),
    C("disk_space", "Low disk space", "error", "Little free space is left on the volume that holds the database.",
      "The database stops accepting writes when the disk is full.",
      "Automatic: prune old agent-run and opportunity history. Free disk space or move backups.", "prune_history",
      (("disk", r""),)),
    C("data_feed", "Market data feed problem", "warning", "A data source reports errors or stale data.",
      "Analyses on that feed are skipped or degraded.", "Check the source in System Monitor → Data sources.", None,
      (("data", r""),)),
    C("api_errors", "API server errors", "error", "The API returned server errors (5xx).",
      "See the latest unhandled exceptions.", "Check the API log; recurring errors are listed as incidents.", None,
      (("api_rate", r""),)),
    C("duplicate_prevented", "Duplicate order prevented", "info",
      "An order was not sent twice: the idempotency check worked.", "No action needed.", "None.", None,
      (("", r"duplicate|idempoten"),)),
    C("strategy_failure", "Strategy evaluation failed", "warning", "A strategy raised an error during evaluation.",
      "That strategy's signal is skipped; others continue.", "Review the strategy's evaluation errors (Strategy Library).",
      None, (("", r"strategy .*(failed|error)|evaluation error"),)),
    C("frontend_error", "Console error", "warning", "The console (browser) hit an unexpected error.",
      "A page component failed; the rest of the console keeps working.", "Reload the page; recurring errors are "
      "reported here with the page and message.", None, (("console", r""),)),
    C("application_exception", "Unexpected application error", "error",
      "An unexpected error occurred in a service.", "See the message and the service log.",
      "Recurring errors are grouped here; report them with the incident details.", None, ()),
]
BY_KEY = {c.key: c for c in CATEGORIES}


def now() -> datetime:
    return datetime.now(UTC)


def classify(component: str, message: str) -> Category:
    comp, msg = (component or "").lower(), (message or "").lower()
    for c in CATEGORIES:
        for cpat, mpat in c.patterns:
            if (not cpat or re.search(cpat, comp)) and (not mpat or re.search(mpat, msg)):
                return c
    return BY_KEY["application_exception"]


def _normalise(message: str) -> str:
    m = re.sub(r"\b[0-9a-f]{8,}\b", "#", (message or "").lower())
    return re.sub(r"\d+(\.\d+)?", "#", m)[:200]


def fingerprint(category: str, component: str, message: str) -> str:
    return hashlib.sha1(f"{category}|{component}|{_normalise(message)}".encode()).hexdigest()


def _publish(inc: Incident, event: str) -> None:
    if SEVERITY.get(inc.severity, 0) < SEVERITY[POPUP_MIN_SEVERITY]:
        return
    from app.services.events import publish
    publish("incident", {"event": event, "id": inc.id, "category": inc.category, "severity": inc.severity,
                         "title": inc.title, "explanation": inc.explanation, "recommended_fix": inc.recommended_fix,
                         "occurrences": inc.occurrences, "status": inc.status})


def record(db: Session, component: str, level: str, message: str, data: dict | None = None,
           category: str | None = None) -> Incident | None:
    """Classify and de-duplicate one problem. ``info`` events are not incidents unless a category says so."""
    cat = BY_KEY[category] if category else classify(component, message)
    severity = level if level in SEVERITY else cat.severity
    if SEVERITY[severity] < SEVERITY[cat.severity] and severity != "info":
        severity = cat.severity
    if severity == "info" and cat.severity != "info":
        return None
    fp = fingerprint(cat.key, component, message)
    inc = db.scalar(select(Incident).where(Incident.fingerprint == fp))
    t = now()
    if inc is None:
        inc = Incident(fingerprint=fp, category=cat.key, severity=severity, component=component[:32],
                       title=cat.title, message=message[:2000], explanation=cat.explanation, diagnosis=cat.diagnosis,
                       recommended_fix=cat.fix, repair=cat.repair, status="OPEN", occurrences=1, reopened=0,
                       data=_safe(data), attempts=[], first_seen=t, last_seen=t, resolution="")
        db.add(inc)
        db.flush()
        _publish(inc, "new")
        return inc
    inc.occurrences += 1
    inc.last_seen, inc.data, inc.message = t, _safe(data), message[:2000]
    if SEVERITY[severity] > SEVERITY.get(inc.severity, 0):
        inc.severity = severity
    if inc.status in ("RESOLVED", "AUTO_RESOLVED"):
        inc.status, inc.reopened, inc.resolved_at, inc.resolution = "OPEN", inc.reopened + 1, None, ""
        inc.attempts = []
        _publish(inc, "reopened")
    if inc.occurrences >= 5 and "recurring" not in inc.diagnosis:
        inc.diagnosis = f"{inc.diagnosis} This is recurring ({inc.occurrences} occurrences)."
    db.flush()
    return inc


def _safe(data: dict | None) -> dict:
    """Recorded data, with credential-like fields removed (same redaction as the AI gateway)."""
    if not data:
        return {}
    try:
        from app.ai.gateway import redact
        out = redact(data)
        json.dumps(out, default=str)
        return json.loads(json.dumps(out, default=str))
    except Exception:  # noqa: BLE001
        return {"note": "data not serialisable"}


def resolve(db: Session, inc: Incident, user_id: int | None, resolution: str, auto: bool = False) -> None:
    inc.status = "AUTO_RESOLVED" if auto else "RESOLVED"
    inc.resolved_at, inc.resolved_by, inc.resolution = now(), user_id, resolution[:1000]
    db.flush()
    from app.services.events import publish
    publish("incident", {"event": "resolved", "id": inc.id, "status": inc.status, "title": inc.title})


# ---- detectors --------------------------------------------------------------------------------------------------------
def _redis():
    from app.services.events import get_redis
    return get_redis()


def bridge_health() -> dict | None:
    try:
        raw = _redis().get("rr:bridge:health")
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def run_detectors(db: Session) -> list[Incident]:
    """Periodic checks for problems no component reports by itself (run by the monitor role)."""
    out: list[Incident] = []

    def add(*a, **k):
        i = record(db, *a, **k)
        if i is not None:
            out.append(i)
    h = bridge_health()
    if h is not None and not h.get("connected"):
        add("bridge", "error", f"MT5 bridge not connected: {str(h.get('error') or 'no detail')[:200]}",
            {"health": h}, category="mt5_disconnected")
    try:
        from app.services.monitoring import backlog
        b = backlog(db)
        for s in b["learner_stuck"]:
            add("monitor", "warning", f"learner job {s['job']} stuck (running since {s['running_since']})", s,
                category="worker_stalled")
    except Exception as exc:  # noqa: BLE001
        log.debug("backlog detector: %s", exc)
    try:
        from app.services import resources
        ov = resources.overview(db)
        for a in ov["alerts"]:
            if a["kind"] == "DISK_SPACE":
                add("disk", "critical" if a["severity"] == "critical" else "warning", a["detail"], {"disk": ov["disk"]},
                    category="disk_space")
            else:
                add("resources", "error" if a["severity"] == "critical" else "warning", a["detail"], {},
                    category="resources_high")
    except Exception as exc:  # noqa: BLE001
        log.debug("resource detector: %s", exc)
    try:
        r = _redis()
        for k in r.scan_iter("rr:ai:cooldown:*"):
            name = (k.decode() if isinstance(k, bytes) else k).split(":", 3)[-1]
            why = r.get(k)
            add("ai", "warning", f"AI provider {name} cooling down ({(why.decode() if isinstance(why, bytes) else why)})",
                {"provider": name}, category="ai_provider")
    except Exception as exc:  # noqa: BLE001
        log.debug("ai detector: %s", exc)
    try:
        from app.db.models_opportunity import DataSource
        for s in db.scalars(select(DataSource).where(DataSource.status == "error", DataSource.enabled.is_(True))):
            add("data", "warning", f"data source {s.name}: {str(s.last_error or 'errors')[:200]}", {"source": s.key},
                category="data_feed")
    except Exception as exc:  # noqa: BLE001
        log.debug("data detector: %s", exc)
    try:
        from app.services.monitoring import _api_window
        w = _api_window(_redis())
        if (w.get("server_errors") or 0) >= 5:
            add("api_rate", "error", f"{w['server_errors']} API server errors in the last {w['window_minutes']} minutes", w,
                category="api_errors")
    except Exception as exc:  # noqa: BLE001
        log.debug("api detector: %s", exc)
    return out


# ---- condition checks (auto-resolve) and the repair catalogue ----------------------------------------------------------
def _healthy_mt5(db, ctx) -> bool | None:
    h = bridge_health()
    return None if h is None else bool(h.get("connected"))


def _healthy_jobs(db, ctx) -> bool | None:
    from app.services.monitoring import backlog
    return not backlog(db)["learner_stuck"]


def _healthy_disk(db, ctx) -> bool | None:
    from app.services import resources
    return resources.disk()["status"] == "OK"


def _healthy_ai(db, ctx) -> bool | None:
    name = (ctx.get("incident").data or {}).get("provider") if ctx.get("incident") else None
    return None if not name else not bool(_redis().exists(f"rr:ai:cooldown:{name}"))


def _healthy_execution(db, ctx) -> bool | None:
    from app.db.models import TradeOrder
    return not db.scalar(select(TradeOrder.id).where(TradeOrder.status.in_(["SENT", "UNKNOWN"])).limit(1))


def _kill_reset(db, ctx) -> bool | None:
    from app.services.trading import get_state
    return not get_state(db).kill_switch


def _lock_reset(db, ctx) -> bool | None:
    from app.services.trading import get_state
    return not get_state(db).risk_lock


# Observations only: an incident closes when its condition is gone. The kill switch and the risk lock are reset by an
# administrator; these checks only notice that it happened.
CHECKS: dict[str, Callable] = {"mt5_disconnected": _healthy_mt5, "worker_stalled": _healthy_jobs,
                               "disk_space": _healthy_disk, "ai_provider": _healthy_ai,
                               "execution_unknown": _healthy_execution, "kill_switch": _kill_reset,
                               "risk_lock": _lock_reset}


def _repair_reconcile(db, ctx) -> tuple[bool, str]:
    bridge = ctx.get("bridge")
    if bridge is None:
        return False, "no connector available in this process"
    from app.execution import manager as em
    res = em.reconcile_unknown(db, bridge)
    return True, f"journal reconciled: {res}"


def _repair_symbols(db, ctx) -> tuple[bool, str]:
    n = 0
    for k in _redis().scan_iter("rr:symbols:refreshed:*"):
        _redis().delete(k)
        n += 1
    return True, f"symbol catalogue refresh requested ({n} broker connection(s)); the monitor rediscovers symbols"


def _repair_jobs(db, ctx) -> tuple[bool, str]:
    from app.db.models_intel import LearningJob
    from app.learning_engine.scheduler import JOBS
    timeouts = {j.name: j.timeout for j in JOBS}
    n = 0
    for row in db.scalars(select(LearningJob).where(LearningJob.status == "RUNNING")):
        limit = timedelta(seconds=2 * timeouts.get(row.job_type, 3600))
        if row.started_at and now() - row.started_at > limit:
            row.status, row.finished_at = "INTERRUPTED", now()
            row.error = "marked interrupted by self-healing: running beyond twice its timeout"
            n += 1
    db.flush()
    return True, f"{n} stuck job(s) marked interrupted (retried at the next period)"


def _repair_prune(db, ctx) -> tuple[bool, str]:
    from app.core.config import get_settings
    from app.orchestration.orchestrator import prune
    from app.scanner.service import prune_opportunities
    s = get_settings()
    a = prune(s.agent_run_retention_days)
    b = prune_opportunities(s.opportunity_retention_days)
    return True, f"pruned {a} agent runs and {b} opportunities beyond their retention"


def _repair_verify_mt5(db, ctx) -> tuple[bool, str]:
    return True, "waiting for the connector: the worker reconnects and reconciles each account by itself"


REPAIRS: dict[str, tuple[Callable, str]] = {
    "reconcile_journal": (_repair_reconcile, "Reconcile the execution journal through the connector (never resubmits)"),
    "refresh_symbols": (_repair_symbols, "Re-discover the broker's symbol catalogue"),
    "recover_stale_jobs": (_repair_jobs, "Mark learner jobs stuck beyond twice their timeout as interrupted"),
    "prune_history": (_repair_prune, "Prune agent-run and opportunity history beyond retention"),
    "verify_mt5": (_repair_verify_mt5, "Re-check the MT5 connection (reconnection is automatic)"),
}
# Categories whose fix must never be automated, whatever the catalogue says.
NEVER_AUTO = {"identity_mismatch", "kill_switch", "risk_lock", "unknown_pending_orders", "order_rejected"}


def auto_repair_enabled(db: Session) -> bool:
    from app.strategies.profiles import policy
    return bool(policy(db).auto_repair)


def attempt(db: Session, inc: Incident, ctx: dict | None = None, user_id: int | None = None) -> dict:
    """Run the incident's catalogue repair once, verify, and record the attempt."""
    ctx = {**(ctx or {}), "incident": inc}
    if not inc.repair or inc.repair not in REPAIRS or inc.category in NEVER_AUTO:
        raise ValueError("no safe automatic repair exists for this incident")
    fn, label = REPAIRS[inc.repair]
    try:
        ok, detail = fn(db, ctx)
    except Exception as exc:  # noqa: BLE001 - a failed repair is recorded, never raised into the caller's loop
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
    check = CHECKS.get(inc.category)
    verified = None
    if ok and check is not None:
        try:
            verified = check(db, ctx)
        except Exception:  # noqa: BLE001
            verified = None
    elif ok and check is None:
        verified = True          # repairs without an observable condition are verified by their own success
    rec = {"at": now().isoformat(), "action": inc.repair, "label": label, "ok": ok, "verified": verified,
           "detail": detail, "by": user_id or "auto"}
    inc.attempts = [*(inc.attempts or []), rec]
    if ok and verified:
        resolve(db, inc, user_id, f"repaired: {label} ({detail})", auto=user_id is None)
    elif len([a for a in inc.attempts if not a.get("verified")]) >= MAX_ATTEMPTS and inc.severity != "critical":
        inc.severity = "critical"
        from app.notifications.service import notify
        notify(db, "SERVER_FAILURE", f"Self-healing could not fix: {inc.title}", f"{inc.message[:300]} - {detail}",
               severity="critical")
        _publish(inc, "escalated")
    db.flush()
    return rec


def heal(db: Session, ctx: dict | None = None) -> dict:
    """One self-healing pass: auto-resolve incidents whose condition cleared, then try catalogue repairs (if the policy
    allows), at most once per ``RETRY_AFTER`` and ``MAX_ATTEMPTS`` times per incident."""
    out = {"auto_resolved": 0, "repairs": 0}
    open_ = db.scalars(select(Incident).where(Incident.status.in_(["OPEN", "ACKNOWLEDGED"]))).all()
    for inc in open_:
        check = CHECKS.get(inc.category)
        if check is not None:
            try:
                healthy = check(db, {**(ctx or {}), "incident": inc})
            except Exception:  # noqa: BLE001
                healthy = None
            if healthy and now() - inc.last_seen > timedelta(seconds=60):
                resolve(db, inc, None, "condition cleared (verified by the detector)", auto=True)
                out["auto_resolved"] += 1
                continue
    if not auto_repair_enabled(db):
        return out
    for inc in db.scalars(select(Incident).where(Incident.status.in_(["OPEN", "ACKNOWLEDGED"]),
                                                 Incident.repair.isnot(None))):
        if inc.category in NEVER_AUTO or len(inc.attempts or []) >= MAX_ATTEMPTS:
            continue
        last = (inc.attempts or [{}])[-1].get("at")
        if last and now() - datetime.fromisoformat(last) < RETRY_AFTER:
            continue
        attempt(db, inc, ctx)
        out["repairs"] += 1
    return out


# ---- learning ---------------------------------------------------------------------------------------------------------
def stats(db: Session, days: int = 30) -> dict:
    """Per category: incidents, occurrences, open, auto-resolved share, mean time to resolve, recurring ones."""
    since = now() - timedelta(days=days)
    rows = db.scalars(select(Incident).where(Incident.last_seen >= since)).all()
    by: dict[str, dict] = {}
    for i in rows:
        d = by.setdefault(i.category, {"category": i.category, "title": BY_KEY.get(i.category, BY_KEY["application_exception"]).title,
                                       "incidents": 0, "occurrences": 0, "open": 0, "auto_resolved": 0, "resolved": 0,
                                       "ttr_minutes": [], "recurring": 0})
        d["incidents"] += 1
        d["occurrences"] += i.occurrences
        d["open"] += i.status in ("OPEN", "ACKNOWLEDGED")
        d["auto_resolved"] += i.status == "AUTO_RESOLVED"
        d["resolved"] += i.status == "RESOLVED"
        d["recurring"] += i.occurrences >= 5 or i.reopened >= 2
        if i.resolved_at:
            d["ttr_minutes"].append((i.resolved_at - i.first_seen).total_seconds() / 60)
    for d in by.values():
        t = d.pop("ttr_minutes")
        d["mean_minutes_to_resolve"] = round(sum(t) / len(t), 1) if t else None
        closed = d["auto_resolved"] + d["resolved"]
        d["self_healed_share"] = round(d["auto_resolved"] / closed, 2) if closed else None
    return {"days": days, "categories": sorted(by.values(), key=lambda x: -x["occurrences"]),
            "open": sum(d["open"] for d in by.values())}


def open_count(db: Session) -> dict:
    rows = db.execute(select(Incident.severity, func.count()).where(Incident.status.in_(["OPEN", "ACKNOWLEDGED"]))
                      .group_by(Incident.severity)).all()
    return {sev: n for sev, n in rows}
