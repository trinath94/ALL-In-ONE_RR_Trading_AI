"""Engine Assistant commands: the safe operator agent (autonomous agent upgrade).

A command typed in the Engine Assistant ("audit my entire engine", "find and repair all errors", "is the engine ready
for autonomous trading?", "check all symbols", "complete the upgrade and hand control back") becomes an ``AgentTask``:

1. **Understand** the command (workflow selection).
2. **Inspect** the engine with its own checks: database and migrations, Redis, services, MT5 connectors and accounts,
   broker symbols, AI providers and budget, risk configuration, trading controls, strategies against the frozen
   baseline, data quality, scanner, learner jobs, open incidents, DEMO validation and LIVE readiness.
3. **Find** problems, each with the component, the evidence, the impact on trading and how it can be fixed: a safe
   catalogue action, a user action (named page or desktop step) or a **repair proposal** for the developer.
4. **Plan** the safe actions; they run only after an authorised user approves the plan in chat.
5. **Execute** each action, **verify** it, **record** it, and write the completion report.

Safety (owner's decision, 2026-11-25): the agent never edits code, applies migrations, enables LIVE, changes risk
limits, changes trading controls, starts or stops the trading worker, or places, modifies or closes orders. The
catalogue holds only actions that cannot change trading. The engine trades by itself; this agent is not needed for it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models_opportunity import AgentTask

UTC = timezone.utc
SEV = {"critical": 0, "error": 1, "warning": 2, "info": 3}

WORKFLOWS = [  # (key, pattern, title) - first match wins
    ("handover", r"hand ?(it )?(control )?(back|over)|handover|complete the (current )?upgrade|finish", "Handover"),
    ("repair", r"repair|fix|heal", "Find and repair errors"),
    ("live_prep", r"\blive\b", "Prepare for LIVE trading"),
    ("demo_test", r"demo", "Test with the DEMO account"),
    ("readiness", r"ready|readiness|autonomous", "Autonomous trading readiness"),
    ("accounts", r"account|broker", "Broker accounts"),
    ("symbols", r"symbol|instrument", "Broker symbols"),
    ("capabilities", r"missing|feature|capabilit|improve|optimi[sz]e|pipeline", "Missing capabilities and weaknesses"),
    ("audit", r"audit|inspect|check|error|scan|whole|entire|full", "Full engine audit"),
]
COMMAND_RE = re.compile(r"\b(audit|inspect|repair|fix|heal|readiness|ready for|handover|hand control|hand it back|"
                        r"complete the upgrade|check all|find (and|all|missing)|improve|optimi[sz]e|prepare|"
                        r"test the engine|test using)\b", re.I)


def is_command(question: str) -> bool:
    return bool(COMMAND_RE.search(question or ""))


def workflow_for(command: str) -> tuple[str, str]:
    for key, pat, title in WORKFLOWS:
        if re.search(pat, command or "", re.I):
            return key, title
    return "audit", "Full engine audit"


def _now() -> datetime:
    return datetime.now(UTC)


def _redis():
    from app.services.events import get_redis
    return get_redis()


def finding(component: str, severity: str, title: str, evidence: str, impact: str, fix_kind: str, fix: str,
            action: str | None = None, target=None) -> dict:
    """fix_kind: action (safe catalogue action the agent can run) | user (a person's step) | proposal (code-level
    repair proposal for the developer) | none."""
    return {"component": component, "severity": severity, "title": title, "evidence": evidence[:600],
            "impact": impact, "fix_kind": fix_kind, "fix": fix, "action": action, "target": target}


# ---- inspection ------------------------------------------------------------------------------------------------------
def _check_database(db) -> list[dict]:
    out = []
    ver = db.execute(text("select version_num from alembic_version")).scalar()
    files = sorted(p.name.split("_", 1)[0] for p in
                   (Path(__file__).resolve().parents[2] / "migrations" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
    head = files[-1] if files else None
    if head and ver != head:
        out.append(finding("database", "critical", "Database schema is not up to date", f"schema {ver}, engine expects {head}",
                           "New features fail and some queries may error.", "user",
                           "Restart the stack (desktop window → Restart All): migrations run at start after a safety backup."))
    return out


def _check_services(db) -> list[dict]:
    out = []
    r = _redis()
    r.ping()
    for role in ("monitor", "analysis"):
        if not r.exists(f"rr:role:{role}:lock"):
            out.append(finding("services", "error", f"The {role} service is not running", f"no {role} heartbeat lock",
                               "No scanning" if role == "analysis" else "No position management, reconciliation or "
                               "self-healing", "user", "Desktop window → Start All (or Restart All)."))
    if not r.exists("rr:learner:lock"):
        out.append(finding("services", "warning", "The learner is not running", "no learner lock",
                           "Learning, reviews and validations pause; trading continues.", "user",
                           "Desktop window → Start Learner."))
    if not r.exists("rr:role:trading:lock"):
        out.append(finding("services", "warning", "The trading worker is stopped", "no trading worker lock",
                           "Nothing is executed automatically.", "user",
                           "Desktop window → Start Trading Worker (type START TRADING WORKER), when you want auto trading."))
    try:
        from app.services import resources
        for a in resources.overview(db)["alerts"]:
            out.append(finding("resources", "error" if a["severity"] == "critical" else "warning", "Resource pressure",
                               a["detail"], "Slow scans; a service can stop.",
                               "action" if a["kind"] == "DISK_SPACE" else "user",
                               "Prune old history" if a["kind"] == "DISK_SPACE" else
                               "Reduce scanned symbols/timeframes or raise the limits (Performance settings).",
                               "prune_history" if a["kind"] == "DISK_SPACE" else None))
    except Exception:  # noqa: BLE001
        pass
    return out


def _check_accounts(db) -> list[dict]:
    from app.core.config import get_settings
    from app.db.models import TradingAccount
    from app.execution import relay
    from app.services import trading as tr
    out = []
    st = tr.get_state(db)
    accts = db.scalars(select(TradingAccount).order_by(TradingAccount.id)).all()
    if not accts:
        out.append(finding("accounts", "critical", "No trading account", "no account is configured",
                           "Nothing can be traded.", "user", "Settings → Trading accounts → add your DEMO account, Verify."))
    for a in accts:
        name = a.label or str(a.login)
        if a.trading_enabled and not a.verified:
            out.append(finding("accounts", "error", f"Account {name} is not verified", "verified=false",
                               "It is not traded.", "user", "Settings → Trading accounts → Verify (MT5 must be connected)."))
        if a.mode == "LIVE" and a.trading_enabled and not a.live_approved_by:
            out.append(finding("accounts", "warning", f"LIVE account {name} is not approved", "no LIVE approval",
                               "It is never traded until an administrator approves it.", "user",
                               "Only if you intend LIVE: Settings → Approve live (typed APPROVE <login>)."))
        if a.trading_enabled and a.mode == st.active_mode and a.verified and not tr.is_reconciled(db, a):
            out.append(finding("accounts", "warning", f"Account {name} is not reconciled", "startup checks not passed",
                               "No new entries on it until its connector is reachable and checks pass.", "action",
                               "Re-run the account's startup reconciliation", "rerun_reconciliation", a.id))
        if get_settings().bridge_mode == "relay" and a.trading_enabled and a.mode == st.active_mode:
            ag = relay.agent_for_account(db, a.login, a.server)
            if ag is None:
                out.append(finding("connector", "error", f"No MT5 connector for account {name}",
                                   f"no RR MT5 Connector reports {a.login}@{a.server}", "The account cannot be traded.",
                                   "user", "MT5 Manager: add a profile for this account's terminal → Use, Link, Start."))
            elif not relay.is_online(ag.id):
                out.append(finding("connector", "error", f"MT5 connector for account {name} is offline",
                                   f"connector {ag.name} has no live connection", "The account cannot be traded.",
                                   "user", "Start MetaTrader 5 and the connector (MT5 Manager → Start)."))
    h = None
    try:
        raw = _redis().get("rr:bridge:health")
        h = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        pass
    if h is not None and not h.get("connected"):
        out.append(finding("connector", "error", "MT5 not connected", str(h.get("error") or "disconnected"),
                           "No market data or execution for the active account.", "user",
                           "Start MetaTrader 5 and the connector; the engine reconnects and reconciles by itself."))
    return out


def symbol_report(db) -> list[dict]:
    from app.db.models import BrokerConnection, Symbol, TradingAccount
    rows = []
    for bc in db.scalars(select(BrokerConnection)):
        q = select(func.count(Symbol.id)).where(Symbol.broker_connection_id == bc.id)
        cats = dict(db.execute(select(Symbol.category, func.count()).where(Symbol.broker_connection_id == bc.id,
                                                                           Symbol.missing_since.is_(None))
                               .group_by(Symbol.category)).all())
        rows.append({"broker_connection": bc.id, "server": bc.server, "broker": bc.broker_name,
                     "accounts": [a.label or str(a.login) for a in db.scalars(select(TradingAccount).where(
                         TradingAccount.broker_connection_id == bc.id))],
                     "symbols": db.scalar(q) or 0,
                     "tradable": db.scalar(q.where(Symbol.tradable.is_(True), Symbol.missing_since.is_(None))) or 0,
                     "scan_enabled": db.scalar(q.where(Symbol.scan_enabled.is_(True))) or 0,
                     "no_longer_offered": db.scalar(q.where(Symbol.missing_since.isnot(None))) or 0,
                     "by_category": cats})
    return rows


def _check_symbols(db) -> list[dict]:
    from app.db.models import Symbol
    out = []
    rep = symbol_report(db)
    if not rep or not any(r["symbols"] for r in rep):
        out.append(finding("symbols", "error", "No broker symbols discovered", "the symbol catalogue is empty",
                           "Nothing can be scanned.", "action", "Re-discover the broker's symbols", "refresh_symbols"))
    elif not any(r["scan_enabled"] for r in rep):
        out.append(finding("symbols", "error", "No symbol is enabled for scanning",
                           f"{sum(r['tradable'] for r in rep)} tradable symbols, none enabled",
                           "The engine analyses nothing.", "user", "Broker Symbols → enable a few symbols you know."))
    bad = db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True),
                                          (Symbol.tradable.is_(False)) | (Symbol.missing_since.isnot(None)))).all()
    if bad:
        out.append(finding("symbols", "warning", "Scanned symbols that cannot be traded",
                           ", ".join(s.broker_symbol for s in bad[:10]), "They are analysed but never executed.",
                           "user", "Broker Symbols → disable them, or check the broker's trading permissions."))
    return out


def _check_ai(db) -> list[dict]:
    from app.ai import gateway
    from app.services import trading as tr
    out = []
    st = tr.get_state(db)
    configured = gateway.configured("candidate_review")
    pol = tr.ai_unavailable_policy(db)
    if (st.ai_mode or "AI") == "AI" and not configured:
        out.append(finding("ai", "error" if pol == "pause" else "warning", "With AI selected but no AI provider",
                           f"no provider for candidate review; unavailable-AI policy: {pol}",
                           "Nothing is auto-executed" if pol == "pause" else "The engine continues without AI review",
                           "user", "AI Studio → add a provider (API key with billing), or choose Without AI / the "
                           "engine-only policy in AI Studio → Active AI."))
    b = gateway.budget_status()
    if b.get("exhausted"):
        out.append(finding("ai", "warning", "AI budget reached", b["exhausted"],
                           "AI calls are skipped; the engine continues" + (" without AI review" if pol == "engine_only"
                                                                         else " but pauses auto-execution in With AI mode"),
                           "user", "AI Studio → Active AI → raise the budget or wait for the next period."))
    for k in _redis().scan_iter("rr:ai:cooldown:*"):
        name = (k.decode() if isinstance(k, bytes) else k).split(":", 3)[-1]
        out.append(finding("ai", "warning", f"AI provider {name} is cooling down", "repeated failures or rate limit",
                           "Its requests pause for a few minutes.", "user",
                           "AI Studio → check the provider's Last call error, key and model."))
    return out


def _check_risk(db) -> list[dict]:
    from app.db.models import TradingAccount
    from app.services import trading as tr
    out = []
    for mode in ("DEMO", "LIVE"):
        errs = tr.risk_params(db, mode).validate()
        if errs:
            out.append(finding("risk", "critical", f"Invalid {mode} risk configuration", "; ".join(errs),
                               "Orders are refused.", "user", "Risk Centre → correct the values."))
    for a in db.scalars(select(TradingAccount).where(TradingAccount.trading_enabled.is_(True))):
        errs = tr.account_risk_params(db, a).validate()
        if errs:
            out.append(finding("risk", "critical", f"Invalid risk limits for account {a.label or a.login}", "; ".join(errs),
                               "Orders on it are refused.", "user", "Settings → account → Risk limits."))
    return out


def _check_controls(db) -> list[dict]:
    from app.services import trading as tr
    st = tr.get_state(db)
    out = []
    if st.kill_switch:
        out.append(finding("controls", "critical", "Kill switch is ON", st.kill_reason or "no reason recorded",
                           "No new entries on any account.", "user",
                           "Review the reason; an administrator resets it in the Risk Centre (never automatic)."))
    if st.risk_lock:
        out.append(finding("controls", "critical", "Risk lock is ON", st.risk_lock_reason,
                           "No new entries.", "user", "Review the limit in the Risk Centre (never reset automatically)."))
    if st.pause_new_entries:
        out.append(finding("controls", "warning", "New entries are paused", "pause_new_entries", "No new entries.",
                           "user", "Live Trading Control Center → Resume entries."))
    if not st.auto_trading:
        out.append(finding("controls", "info", "Auto trading is OFF", "auto_trading=false",
                           "Signals are produced but not executed.", "user",
                           "Live Trading Control Center → Start auto (DEMO), when you want autonomous trading."))
    return out


def _check_strategies(db) -> list[dict]:
    from app.services import strategy_library as lib
    from app.strategies import registry
    out = []
    n = len(registry.all_strategies())
    if n < 215:
        out.append(finding("strategies", "critical", "Strategies missing from the registry", f"{n} of 215",
                           "Fewer strategies than the baseline.", "proposal",
                           "Restore the missing strategy modules from the baseline (developer)."))
    st = lib.baseline_status()
    if st is not None:
        mod = sorted(c for c, v in st.items() if v == "modified")
        if mod:
            out.append(finding("strategies", "warning", "Strategies differ from the frozen baseline", ", ".join(mod[:10]),
                               "Their behaviour may have changed.", "proposal",
                               "Review the changes against the baseline inventory (developer)."))
    return out


def _check_engine_runtime(db) -> list[dict]:
    out = []
    try:
        from app.core.config import get_settings
        from app.scanner import scheduler as sched
        raw = _redis().get(sched.STATUS_KEY)
        st = json.loads(raw) if raw else None
        if st is None:
            out.append(finding("scanner", "warning", "No scan recorded", "no scanner status",
                               "The analysis service may not be running.", "user", "Desktop window → Start All."))
        else:
            if (st.get("errors") or 0) + (st.get("timed_out") or 0):
                out.append(finding("scanner", "warning", "The last scan had errors",
                                   f"{st.get('errors')} errors, {st.get('timed_out')} timed out", "Some analyses were skipped.",
                                   "action", "Run the detectors and safe repairs", "run_detectors"))
            fin = st.get("finished_at")
            if fin:
                age = (_now() - datetime.fromisoformat(fin)).total_seconds()
                if age > 3 * max(get_settings().scan_interval_seconds, 60):
                    out.append(finding("scanner", "error", "Scanning has stopped", f"last scan {int(age // 60)} min ago",
                                       "No new signals.", "user", "Desktop window → Restart All."))
    except Exception:  # noqa: BLE001
        pass
    from app.services.monitoring import backlog
    b = backlog(db)
    for s in b["learner_stuck"]:
        out.append(finding("learner", "warning", f"Learner job {s['job']} stuck", f"running since {s['running_since']}",
                           "Its learning step does not progress.", "action", "Recover stuck learner jobs",
                           "recover_stale_jobs"))
    from app.db.models import AnalysisResult, Symbol
    latest = select(AnalysisResult.symbol_id, AnalysisResult.timeframe, func.max(AnalysisResult.id).label("id")) \
        .group_by(AnalysisResult.symbol_id, AnalysisResult.timeframe).subquery()
    for a, name in db.execute(select(AnalysisResult, Symbol.broker_symbol).join(latest, AnalysisResult.id == latest.c.id)
                              .join(Symbol, Symbol.id == AnalysisResult.symbol_id)).all()[:200]:
        score = ((a.structure or {}).get("data_quality") or {}).get("score")
        if score is not None and score < 70:
            out.append(finding("data", "warning", f"Low market data quality {name} {a.timeframe}", f"score {score}",
                               "Analyses on it are less reliable.", "user",
                               "System Monitor → Market data quality: check the broker feed for this symbol."))
    return out


def _check_incidents(db) -> list[dict]:
    from app.db.models_opportunity import Incident
    from app.services import incidents
    out = []
    for i in db.scalars(select(Incident).where(Incident.status.in_(["OPEN", "ACKNOWLEDGED"]))
                        .order_by(Incident.last_seen.desc()).limit(25)):
        if i.repair and i.category not in incidents.NEVER_AUTO and len(i.attempts or []) < incidents.MAX_ATTEMPTS:
            kind, act = "action", "incident_repair"
        elif i.category in ("application_exception", "frontend_error", "api_errors", "strategy_failure"):
            kind, act = "proposal", None
        else:
            kind, act = "user", None
        out.append(finding(i.component, i.severity, i.title, f"{i.message[:300]} ({i.occurrences}×)",
                           i.explanation, kind, i.recommended_fix if kind != "proposal" else
                           f"Code-level error, {i.occurrences} occurrence(s) since {i.first_seen:%Y-%m-%d %H:%M}: "
                           f"fix in the development pipeline. {i.recommended_fix}", act, i.id))
    return out


CHECKS = {"database": _check_database, "services": _check_services, "accounts": _check_accounts,
          "symbols": _check_symbols, "ai": _check_ai, "risk": _check_risk, "controls": _check_controls,
          "strategies": _check_strategies, "runtime": _check_engine_runtime, "incidents": _check_incidents}
SCOPE = {"audit": list(CHECKS), "repair": list(CHECKS), "readiness": list(CHECKS), "capabilities": list(CHECKS),
         "handover": list(CHECKS), "accounts": ["accounts", "risk"], "symbols": ["symbols"],
         "demo_test": ["services", "accounts", "symbols", "ai", "risk", "controls", "incidents"],
         "live_prep": ["accounts", "risk", "controls", "incidents"]}


def inspect(db: Session, scope: list[str]) -> tuple[list[dict], dict]:
    findings, ran = [], {}
    for name in scope:
        try:
            with db.begin_nested():
                f = CHECKS[name](db)
            findings += f
            ran[name] = "ok" if not f else f"{len(f)} finding(s)"
        except Exception as exc:  # noqa: BLE001 - a failing check is itself a finding
            ran[name] = f"check failed: {type(exc).__name__}"
            findings.append(finding(name, "error", f"The {name} check could not run", f"{type(exc).__name__}: {exc}"[:400],
                                    "That part of the engine could not be verified.", "proposal",
                                    "Investigate the error in the engine log (developer)."))
    findings.sort(key=lambda x: SEV.get(x["severity"], 9))
    return findings, ran


# ---- safe action catalogue ------------------------------------------------------------------------------------------
def _act_incident(db, target):
    from app.db.models_opportunity import Incident
    from app.services import incidents
    i = db.get(Incident, target)
    if i is None:
        return False, None, "incident no longer exists"
    rec = incidents.attempt(db, i, {})
    return rec["ok"], rec["verified"], rec["detail"]


def _act_symbols(db, target):
    from app.services.incidents import _repair_symbols
    ok, detail = _repair_symbols(db, {})
    return ok, None, detail + " (verified at the next monitor cycle)"


def _act_jobs(db, target):
    from app.services.incidents import _healthy_jobs, _repair_jobs
    ok, detail = _repair_jobs(db, {})
    return ok, _healthy_jobs(db, {}), detail


def _act_prune(db, target):
    from app.services.incidents import _repair_prune
    ok, detail = _repair_prune(db, {})
    return ok, True, detail


def _act_reconcile(db, target):
    from app.db.models import TradingAccount
    from app.services import trading as tr
    a = db.get(TradingAccount, target)
    if a is None:
        return False, None, "account no longer exists"
    tr.set_reconciled(db, a, False)
    return True, None, (f"account {a.label or a.login}: startup reconciliation requested; the monitor runs it through "
                        "the account's connector (new entries wait for it)")


def _act_detectors(db, target):
    from app.services import incidents
    found = incidents.run_detectors(db)
    healed = incidents.heal(db, {})
    return True, True, f"{len(found)} problem(s) detected; {healed['auto_resolved']} resolved, {healed['repairs']} repaired"


ACTIONS = {
    "incident_repair": (_act_incident, "Run the incident's safe repair"),
    "refresh_symbols": (_act_symbols, "Re-discover the broker's symbols"),
    "recover_stale_jobs": (_act_jobs, "Recover stuck learner jobs"),
    "prune_history": (_act_prune, "Prune history beyond retention"),
    "rerun_reconciliation": (_act_reconcile, "Re-run an account's startup reconciliation"),
    "run_detectors": (_act_detectors, "Run the detectors and safe repairs"),
}


# ---- tasks -----------------------------------------------------------------------------------------------------------
def _runtime(db) -> dict:
    from app.ai import gateway
    from app.services import trading as tr
    st = tr.get_state(db)
    r = _redis()
    roles = {role: bool(r.exists(f"rr:role:{role}:lock")) for role in ("monitor", "analysis", "trading")}
    roles["learner"] = bool(r.exists("rr:learner:lock"))
    return {"services_running": roles, "mode": st.active_mode, "auto_trading": st.auto_trading,
            "engine_ai_mode": "with AI" if (st.ai_mode or "AI") == "AI" else "without AI",
            "ai_unavailable_policy": tr.ai_unavailable_policy(db), "ai_budget": gateway.budget_status(),
            "independent_of_ai": (st.ai_mode == "ENGINE_ONLY") or tr.ai_unavailable_policy(db) == "engine_only",
            "note": "Scanning, signals, the Risk Engine, execution, position management, reconciliation, learning and "
                    "self-healing run inside the engine; no AI call is needed for them."}


def _ai_usage(db, days: int = 30) -> dict:
    from app.db.models_ai import AIUsage
    since = _now() - timedelta(days=days)
    rows = db.execute(select(AIUsage.task, AIUsage.status, func.count(), func.coalesce(func.sum(AIUsage.cost), 0.0),
                             func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0))
                      .where(AIUsage.created_at >= since).group_by(AIUsage.task, AIUsage.status)).all()
    by: dict = {}
    for task, status, n, cost, tok in rows:
        d = by.setdefault(task, {"calls": 0, "cached": 0, "skipped": 0, "errors": 0, "cost_usd": 0.0, "tokens": 0})
        key = "calls" if status == "ok" else "cached" if status == "cached" else "skipped" if status == "skipped" else "errors"
        d[key] += n
        d["cost_usd"] = round(d["cost_usd"] + float(cost or 0), 6)
        d["tokens"] += int(tok or 0)
    return {"days": days, "by_task": by, "cost_usd": round(sum(v["cost_usd"] for v in by.values()), 6),
            "tokens": sum(v["tokens"] for v in by.values())}


def _summary(findings: list[dict]) -> dict:
    out = {"total": len(findings), "by_severity": {}, "by_fix": {}}
    for f in findings:
        out["by_severity"][f["severity"]] = out["by_severity"].get(f["severity"], 0) + 1
        out["by_fix"][f["fix_kind"]] = out["by_fix"].get(f["fix_kind"], 0) + 1
    return out


def start(db: Session, command: str, user_id: int | None) -> AgentTask:
    """Inspect, record the findings and the plan of safe actions. Nothing is changed here."""
    wf, title = workflow_for(command)
    findings, ran = inspect(db, SCOPE[wf])
    plan = []
    if wf in ("repair", "audit", "readiness", "handover", "demo_test", "symbols", "accounts", "capabilities"):
        seen = set()
        for f in findings:
            if f["fix_kind"] == "action" and f["action"] in ACTIONS and (f["action"], f["target"]) not in seen:
                seen.add((f["action"], f["target"]))
                plan.append({"action": f["action"], "label": ACTIONS[f["action"]][1], "target": f["target"],
                             "for": f["title"], "status": "PENDING", "result": None, "verified": None})
    report: dict = {"title": title, "checks": ran, "summary": _summary(findings), "runtime": _runtime(db)}
    if wf == "symbols":
        report["symbols"] = symbol_report(db)
    if wf in ("readiness", "demo_test", "handover", "live_prep"):
        from app.services import demo_validation, readiness
        report["demo_validation"] = {k: demo_validation.report(db).get(k) for k in ("accepted", "counts", "items")}
        report["live_readiness"] = readiness.report(db)
    if wf == "live_prep":
        report["note"] = ("The agent never enables LIVE. LIVE needs: an approved LIVE account, the readiness checklist, "
                          "the typed ENABLE LIVE TRADING switch, the PC's own LIVE lock, then auto trading in LIVE.")
    if wf == "handover":
        report.update(_handover(db))
    report["repair_proposals"] = [f for f in findings if f["fix_kind"] == "proposal"]
    report["user_actions"] = [f for f in findings if f["fix_kind"] == "user"]
    t = AgentTask(command=command[:2000], workflow=wf, status="AWAITING_APPROVAL" if plan else "DONE",
                  findings=json.loads(json.dumps(findings, default=str)), plan=plan,
                  report=json.loads(json.dumps(report, default=str)), created_by=user_id,
                  finished_at=None if plan else _now())
    db.add(t)
    db.flush()
    return t


def approve(db: Session, task: AgentTask, user_id: int | None) -> AgentTask:
    """Run the approved plan: each safe action once, verified and recorded, then the completion report."""
    if task.status != "AWAITING_APPROVAL":
        raise ValueError(f"the task is {task.status}")
    task.status, task.approved_by = "RUNNING", user_id
    steps = []
    for s in task.plan or []:
        fn = ACTIONS[s["action"]][0]
        try:
            with db.begin_nested():
                ok, verified, detail = fn(db, s["target"])
        except Exception as exc:  # noqa: BLE001
            ok, verified, detail = False, None, f"{type(exc).__name__}: {str(exc)[:200]}"
        steps.append({**s, "status": "DONE" if ok else "FAILED", "result": detail, "verified": verified,
                      "at": _now().isoformat()})
    task.plan = steps
    after, ran = inspect(db, SCOPE[task.workflow])
    before = {(f["title"], f["component"]) for f in task.findings or []}
    now_ = {(f["title"], f["component"]) for f in after}
    task.report = json.loads(json.dumps({**(task.report or {}), "executed": steps,
                                         "resolved": sorted(t for t, _ in before - now_),
                                         "still_open": sorted(t for t, _ in before & now_),
                                         "after": _summary(after), "runtime": _runtime(db)}, default=str))
    task.status = "DONE" if all(s["status"] == "DONE" for s in steps) else "FAILED"
    task.finished_at = _now()
    db.flush()
    return task


def cancel(db: Session, task: AgentTask) -> AgentTask:
    if task.status == "AWAITING_APPROVAL":
        task.status, task.finished_at = "CANCELLED", _now()
    return task


def _handover(db) -> dict:
    """What the owner hands back to the engine: recent repairs, symbol discovery, runtime, AI usage, readiness."""
    since = _now() - timedelta(days=7)
    tasks = db.scalars(select(AgentTask).where(AgentTask.created_at >= since).order_by(AgentTask.id)).all()
    executed = [s for t in tasks for s in (t.plan or []) if s.get("status") in ("DONE", "FAILED")]
    return {"recent_tasks": [{"id": t.id, "command": t.command[:120], "workflow": t.workflow, "status": t.status}
                             for t in tasks[-20:]],
            "repairs_executed": executed, "symbol_discovery": symbol_report(db), "ai_usage": _ai_usage(db),
            "not_done_by_the_agent": ["code changes, migrations and tests run in the development pipeline and arrive "
                                      "with the next installer", "enabling LIVE, changing risk limits or trading "
                                      "controls, starting or stopping the trading worker, placing orders"],
            "handover": "The engine's runtime (scanner, Risk Engine, execution, position management, reconciliation, "
                        "learning, self-healing) operates by itself; the agent session can end."}


def describe(task: AgentTask) -> str:
    """The chat text for a task."""
    r = task.report or {}
    s = r.get("summary", {})
    lines = [f"{r.get('title', task.workflow)} — task #{task.id} ({task.status.replace('_', ' ').lower()})",
             f"Checks: " + ", ".join(f"{k} {v}" for k, v in (r.get("checks") or {}).items()),
             f"Findings: {s.get('total', 0)}" + (" (" + ", ".join(f"{v} {k}" for k, v in s.get("by_severity", {}).items()) + ")"
                                                 if s.get("total") else " — nothing wrong found")]
    for f in (task.findings or [])[:12]:
        how = {"action": "the agent can fix it (needs your approval)", "user": "your step", "proposal":
               "repair proposal for the developer", "none": ""}[f["fix_kind"]]
        lines.append(f"• [{f['severity']}] {f['title']}: {f['evidence']}. {f['impact']} → {f['fix']} ({how})")
    if len(task.findings or []) > 12:
        lines.append(f"… and {len(task.findings) - 12} more (see the task).")
    if task.status == "AWAITING_APPROVAL":
        lines.append(f"Plan: {len(task.plan)} safe action(s): " + "; ".join(f"{p['label']}" for p in task.plan)
                     + ". Approve to run them; each is verified and recorded.")
    if r.get("executed"):
        lines.append("Executed: " + "; ".join(f"{s['label']} → {s['status'].lower()}"
                                             f"{' (verified)' if s.get('verified') else ''}" for s in r["executed"]))
        if r.get("resolved"):
            lines.append("Resolved: " + "; ".join(r["resolved"]))
    if r.get("symbols"):
        for b in r["symbols"]:
            lines.append(f"Broker {b['broker']} {b['server']}: {b['symbols']} symbols, {b['tradable']} tradable, "
                         f"{b['scan_enabled']} scanned ({', '.join(f'{k} {v}' for k, v in b['by_category'].items())})")
    if r.get("live_readiness"):
        lr = r["live_readiness"]
        lines.append("LIVE readiness: " + ("ready (LIVE stays locked until you enable it)" if lr["ready"]
                                           else "not ready: " + ", ".join(lr["failing"])))
    if r.get("note"):
        lines.append(r["note"])
    if r.get("handover"):
        u = r["ai_usage"]
        lines.append(f"AI usage (30 days): {u['cost_usd']} USD, {u['tokens']} tokens. {r['handover']}")
    return "\n".join(lines)
