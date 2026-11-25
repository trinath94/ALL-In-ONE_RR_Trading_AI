"""Engine Assistant: a chat agent that explains what the engine is doing and what to do next.

It reads the engine's own records (control state, accounts and connectors, the scanner's last cycle, signals, AI
reviews, the Risk Engine's evaluations, the execution journal, opportunities and incidents) and walks a signal through
the same gates the trading worker applies, in the same order, to show where a valid signal stopped and the next step.

Read-only: it never changes a setting, places, modifies or closes an order, or starts or stops anything. Its guidance
names the console page or the desktop action a person uses. With an AI provider configured (task
``engine_assistant``), the AI phrases the answer from these facts only; without one, or if the AI fails, the engine's
own answer is returned. Facts are never invented: what is not recorded is reported as unknown.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import RiskEvaluation, Symbol, TradeOrder, TradingAccount, TradingSignal
from app.services import trading as tr

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _redis():
    from app.services.events import get_redis
    return get_redis()


def worker_running() -> bool | None:
    try:
        return bool(_redis().exists("rr:role:trading:lock"))
    except Exception:  # noqa: BLE001
        return None


def _bridge_health() -> dict | None:
    try:
        raw = _redis().get("rr:bridge:health")
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def _scanner() -> dict | None:
    try:
        from app.scanner import scheduler as sched
        raw = _redis().get(sched.STATUS_KEY)
        st = json.loads(raw) if raw else None
        return {k: st.get(k) for k in ("finished_at", "symbols", "timeframes", "due", "analysed", "errors", "timed_out",
                                       "insufficient_data")} if st else None
    except Exception:  # noqa: BLE001
        return None


def _ai_status(db: Session) -> dict:
    from app.ai import gateway
    hops = gateway.chain_for("candidate_review")
    single = gateway.single_active_chain(db)
    return {"configured": bool(hops), "providers": [f"{h.name}/{h.model}" for h in hops],
            "single_active": single is not None}


# ---- one signal through the worker's gates ----------------------------------------------------------------------------
def diagnose_signal(db: Session, sig: TradingSignal) -> dict:
    """Each gate the trading worker applies, in order, with its result and the next step when it blocks."""
    st = tr.get_state(db)
    sym = db.get(Symbol, sig.symbol_id)
    checks: list[dict] = []

    def add(key, label, ok, detail, nxt=None):
        checks.append({"check": key, "label": label, "ok": ok, "detail": detail, "next_step": None if ok else nxt})
    orders = db.scalars(select(TradeOrder).where(TradeOrder.signal_id == sig.id).order_by(TradeOrder.id)).all()
    confirmed = [o for o in orders if o.status == "CONFIRMED"]
    add("decision", "Engine decision", sig.decision in ("BUY", "SELL"),
        f"{sig.decision} {sig.grade} (score {sig.score})",
        "No trade by design: the engine only trades BUY or SELL setups. Wait for the next setup.")
    expired = sig.expires_at is not None and sig.expires_at <= _now() and not confirmed
    add("fresh", "Signal still valid", not expired,
        f"expires {sig.expires_at.isoformat() if sig.expires_at else 'never'}; status {sig.status}",
        "The signal expired before execution. Keep the trading worker running and auto trading on so the next "
        "signal is picked up within its validity.")
    run = worker_running()
    add("worker", "Trading worker running", run is not False,
        "running" if run else ("not running" if run is False else "unknown (Redis unreachable)"),
        "Start it in the RR Trading One desktop window: Start Trading Worker (type START TRADING WORKER).")
    add("auto_trading", "Auto trading", st.auto_trading, "ON" if st.auto_trading else "OFF",
        "Trading Console → Live Trading Control Center → Start auto (DEMO).")
    if st.auto_trading and st.auto_trading_since and sig.created_at < st.auto_trading_since:
        add("since", "Produced after auto trading started", False,
            f"signal {sig.created_at.isoformat()}, auto trading since {st.auto_trading_since.isoformat()}",
            "By design, signals from before auto trading was switched on are never executed. Wait for a new signal.")
    review = sig.ai_review or {}
    mode = st.ai_mode or "AI"
    if mode == "ENGINE_ONLY":
        add("ai", "AI review (engine runs without AI)", review.get("decision") == "SKIPPED",
            "engine-only mode: no AI review needed" if review.get("decision") == "SKIPPED"
            else f"produced while AI mode was on (review {review.get('decision')})",
            "This signal was produced in AI mode; only signals produced without AI are executed in this mode. Wait "
            "for a new signal.")
    else:
        d = review.get("decision")
        ai = _ai_status(db)
        if d in ("BUY", "SELL") and d == sig.decision:
            add("ai", "AI review", True, f"confirmed {d}")
        elif not ai["configured"]:
            add("ai", "AI review", False, "no AI provider is configured: candidates stay unreviewed",
                "Add a provider in AI Studio (an API key with API billing), or switch the engine to Without AI in the "
                "Live Trading Control Center.")
        elif d is None:
            add("ai", "AI review", False, "not reviewed (not on the shortlist of the strongest candidates of its scan)",
                "Only the best-scored candidates of each scan are reviewed. Nothing to fix; stronger setups are "
                "reviewed first.")
        elif d == "UNREVIEWED":
            add("ai", "AI review", False, f"review failed: {str(review.get('error') or 'no answer')[:200]}",
                "Check the provider in AI Studio (Last call, Test); consider a fallback provider in Active AI.")
        else:
            why = "; ".join((review.get("conflicts") or [])[:2]) or review.get("explanation") or ""
            add("ai", "AI review", False, f"AI decided {d}{': ' + str(why)[:200] if why else ''}",
                "The AI did not confirm this setup. That is a valid outcome; no action needed unless it happens to "
                "every signal (then review the provider and model in AI Studio).")
    add("kill_switch", "Kill switch", not st.kill_switch, st.kill_reason or "off",
        "An administrator resets it in the Risk Centre after reviewing the reason.")
    add("risk_lock", "Risk lock", not st.risk_lock, st.risk_lock_reason or "off",
        "A risk limit was reached; an administrator reviews it in the Risk Centre (never reset automatically).")
    add("pause", "New entries", not st.pause_new_entries, "paused" if st.pause_new_entries else "allowed",
        "Trading Console → Live Trading Control Center → Resume entries.")
    accounts = tr.execution_accounts(db)
    add("accounts", "Execution accounts", bool(accounts),
        ", ".join(f"{a.label or a.login} ({a.mode})" for a in accounts) or f"none in {st.active_mode}",
        "Settings → Trading accounts: verify an account, Activate it (or Settings → Enable trading).")
    per_account = []
    codes = sorted({x.get("strategy") for x in (sig.strategies or []) if isinstance(x, dict) and x.get("strategy")})
    for a in accounts:
        blk = tr.account_block(a, sym.broker_symbol if sym else "", codes)
        rec = tr.is_reconciled(db, a)
        ev = db.scalar(select(RiskEvaluation).where(RiskEvaluation.signal_id == sig.id, RiskEvaluation.account_id == a.id)
                       .order_by(RiskEvaluation.id.desc()).limit(1))
        o = next((x for x in reversed(orders) if x.account_id == a.id), None)
        row = {"account": a.label or str(a.login), "mode": a.mode, "eligible": blk is None, "eligibility": blk,
               "reconciled": rec, "risk_engine": None if ev is None else ("APPROVED" if ev.approved else "REJECTED"),
               "risk_reasons": list(ev.reasons or [])[:5] if ev is not None and not ev.approved else [],
               "order": None if o is None else {"status": o.status, "detail": o.status_detail, "volume": float(o.volume)}}
        if blk:
            row["next_step"] = "Settings → Trading accounts → Settings: adjust the account's symbols or strategies."
        elif not rec:
            row["next_step"] = ("The account is reconciled at start through its own connector: check that its MT5 "
                                "terminal and connector are running (MT5 Manager).")
        elif ev is not None and not ev.approved:
            row["next_step"] = ("The Risk Engine refused it for the reasons shown. Change limits in the Risk Centre "
                                "only if they are wrong; a refusal protects the account.")
        elif o is not None and o.status in ("REJECTED", "FAILED"):
            row["next_step"] = "The broker or connector refused the order; see the detail and the MT5 journal."
        elif o is not None and o.status in ("SENT", "UNKNOWN"):
            row["next_step"] = "Outcome unknown: the monitor reconciles it with the connector (never resubmitted)."
        per_account.append(row)
    blocking = next((c for c in checks if c["ok"] is False), None)
    if confirmed:
        verdict = f"executed on {len({o.account_id for o in confirmed})} account(s)"
        stopped = None
    elif blocking is not None:
        verdict, stopped = f"not executed: stopped at '{blocking['label']}'", blocking
    elif per_account and not any(r.get("order") for r in per_account) and all(r.get("risk_engine") is None for r in per_account):
        verdict = "eligible: waiting for the trading worker to pick it up"
        stopped = None
    else:
        verdict = "not executed on any account (see each account)"
        stopped = next(({"label": r["account"], "next_step": r.get("next_step")} for r in per_account if r.get("next_step")), None)
    opp = None
    try:
        from app.db.models_opportunity import Opportunity
        o2 = db.scalar(select(Opportunity).where(Opportunity.signal_id == sig.id).order_by(Opportunity.id.desc()).limit(1))
        if o2 is not None:
            opp = {"id": o2.id, "state": o2.state, "reason": o2.state_reason}
    except Exception:  # noqa: BLE001
        pass
    return {"signal": {"id": sig.id, "symbol": sym.broker_symbol if sym else None, "timeframe": sig.timeframe,
                       "decision": sig.decision, "grade": sig.grade, "status": sig.status,
                       "created_at": sig.created_at, "expires_at": sig.expires_at},
            "verdict": verdict, "checks": checks, "accounts": per_account, "opportunity": opp,
            "next_step": (stopped or {}).get("next_step")}


# ---- the whole engine -------------------------------------------------------------------------------------------------
def overview(db: Session) -> dict:
    st = tr.get_state(db)
    since = _now() - timedelta(hours=24)
    by = {f"{d}/{s}": n for d, s, n in db.execute(
        select(TradingSignal.decision, TradingSignal.status, func.count()).where(TradingSignal.created_at >= since)
        .group_by(TradingSignal.decision, TradingSignal.status))}
    accts = []
    for a in db.scalars(select(TradingAccount).order_by(TradingAccount.id)):
        accts.append({"account": a.label or str(a.login), "mode": a.mode, "active": a.is_active,
                      "trading_enabled": a.trading_enabled, "verified": a.verified,
                      "reconciled": tr.is_reconciled(db, a) if a.trading_enabled else None})
    try:
        from app.services import incidents
        from app.db.models_opportunity import Incident
        open_inc = [{"title": i.title, "severity": i.severity, "count": i.occurrences, "fix": i.recommended_fix}
                    for i in db.scalars(select(Incident).where(Incident.status.in_(["OPEN", "ACKNOWLEDGED"]))
                                        .order_by(Incident.last_seen.desc()).limit(5))]
        _ = incidents
    except Exception:  # noqa: BLE001
        open_inc = []
    return {"mode": st.active_mode, "engine_ai_mode": "with AI" if (st.ai_mode or "AI") == "AI" else "without AI",
            "auto_trading": st.auto_trading, "worker_running": worker_running(), "kill_switch": st.kill_switch,
            "kill_reason": st.kill_reason, "risk_lock": st.risk_lock, "risk_lock_reason": st.risk_lock_reason,
            "new_entries_paused": st.pause_new_entries, "active_account_reconciled": st.startup_checks_passed,
            "accounts": accts, "mt5_bridge": _bridge_health(), "scanner_last_cycle": _scanner(), "ai": _ai_status(db),
            "scan_enabled_symbols": db.scalar(select(func.count(Symbol.id)).where(Symbol.scan_enabled.is_(True))) or 0,
            "signals_24h": by, "open_incidents": open_inc}


def next_steps(ov: dict) -> list[str]:
    """What blocks automatic trading now, most important first."""
    out = []
    if ov["kill_switch"]:
        out.append(f"Kill switch is ON ({ov['kill_reason'] or 'no reason recorded'}): review it; an administrator resets "
                   "it in the Risk Centre.")
    if ov["risk_lock"]:
        out.append(f"Risk lock is ON ({ov['risk_lock_reason']}): review the limit in the Risk Centre.")
    b = ov["mt5_bridge"]
    if b is not None and not b.get("connected"):
        out.append("MT5 is not connected: start MetaTrader 5 and the connector (desktop window → MT5 Manager → Start).")
    if not any(a["trading_enabled"] and a["verified"] for a in ov["accounts"]):
        out.append("No verified trading account: Settings → Trading accounts → add, Verify, Activate.")
    if ov["worker_running"] is False:
        out.append("The trading worker is stopped: desktop window → Start Trading Worker.")
    if not ov["auto_trading"]:
        out.append("Auto trading is OFF: Live Trading Control Center → Start auto.")
    if ov["new_entries_paused"]:
        out.append("New entries are paused: Live Trading Control Center → Resume entries.")
    if ov["engine_ai_mode"] == "with AI" and not ov["ai"]["configured"]:
        out.append("With AI is selected but no AI provider is configured: add one in AI Studio, or choose Without AI.")
    if not ov["scan_enabled_symbols"]:
        out.append("No symbol is enabled for scanning: Broker Symbols → enable a few symbols.")
    sc = ov["scanner_last_cycle"]
    if sc and (sc.get("errors") or 0) > 0:
        out.append(f"The last scan had {sc['errors']} error(s): see System Monitor → Incidents.")
    for i in ov["open_incidents"][:3]:
        if i["severity"] in ("critical", "error"):
            out.append(f"Open incident: {i['title']} → {i['fix']}")
    return out or ["Nothing is blocking automatic trading. Signals are executed when a valid setup appears and the "
                   "Risk Engine approves it."]


def latest_valid_signal(db: Session) -> TradingSignal | None:
    return db.scalar(select(TradingSignal).where(TradingSignal.decision.in_(["BUY", "SELL"]),
                                                 TradingSignal.created_at >= _now() - timedelta(days=3))
                     .order_by(TradingSignal.id.desc()).limit(1))


# ---- answering ---------------------------------------------------------------------------------------------------------
SIGNAL_RE = re.compile(r"(?:signal\s*#?|#)\s*(\d{1,10})", re.IGNORECASE)
WHY_RE = re.compile(r"why|not (be )?execut|no trade|didn'?t|did not|blocked|stuck|missed|not trad|isn'?t trading", re.I)
NEXT_RE = re.compile(r"what (should|do|to)|next|help|guide|how (do|can) i|fix", re.I)


def _engine_text(intent: str, ov: dict, diag: dict | None, steps: list[str]) -> str:
    lines = []
    if diag is not None:
        s = diag["signal"]
        lines.append(f"Signal #{s['id']} {s['symbol']} {s['timeframe']} {s['decision']} ({s['grade']}): {diag['verdict']}.")
        for c in diag["checks"]:
            lines.append(f"{'✔' if c['ok'] else ('✖' if c['ok'] is False else '?')} {c['label']}: {c['detail']}")
        for a in diag["accounts"]:
            bits = [f"eligible" if a["eligible"] else f"not eligible ({a['eligibility']})",
                    "reconciled" if a["reconciled"] else "not reconciled"]
            if a["risk_engine"]:
                bits.append(f"Risk Engine {a['risk_engine']}" + (f" ({'; '.join(a['risk_reasons'][:2])})" if a["risk_reasons"] else ""))
            if a["order"]:
                bits.append(f"order {a['order']['status']}" + (f": {a['order']['detail']}" if a["order"]["detail"] else ""))
            lines.append(f"Account {a['account']}: " + ", ".join(bits))
        if diag["next_step"]:
            lines.append(f"Next step: {diag['next_step']}")
        return "\n".join(lines)
    if intent == "why":
        lines.append("There is no BUY or SELL signal in the last 3 days, so nothing was due for execution.")
        sc = ov["scanner_last_cycle"]
        lines.append(f"Scanner: {ov['scan_enabled_symbols']} symbol(s) enabled; last cycle "
                     + (f"{sc.get('finished_at')}, {sc.get('analysed')} analysed, {sc.get('errors')} error(s)."
                        if sc else "not recorded (is the analysis service running?)."))
        lines.append("Most analyses end in WAIT or NO_TRADE: the engine only trades when its strategies agree and the "
                     "quality gates pass.")
    else:
        lines.append(f"Mode {ov['mode']}, engine {ov['engine_ai_mode']}, auto trading {'ON' if ov['auto_trading'] else 'OFF'}, "
                     f"trading worker {'running' if ov['worker_running'] else 'stopped' if ov['worker_running'] is False else 'unknown'}.")
        b = ov["mt5_bridge"]
        lines.append("MT5: " + ("unknown (no recent heartbeat)" if b is None else "connected" if b.get("connected")
                                else f"not connected ({b.get('error', '')})"))
        lines.append("Accounts: " + (", ".join(f"{a['account']} {a['mode']}{' active' if a['active'] else ''}"
                                               f"{' trading' if a['trading_enabled'] else ' research'}" for a in ov["accounts"]) or "none"))
        ai = ov["ai"]
        lines.append("AI: " + (", ".join(ai["providers"]) if ai["configured"] else "no provider configured")
                     + (" (single active)" if ai["single_active"] else ""))
        sig = ov["signals_24h"]
        lines.append("Signals (24 h): " + (", ".join(f"{k} {v}" for k, v in sorted(sig.items())) or "none"))
        if ov["open_incidents"]:
            lines.append("Open incidents: " + "; ".join(f"{i['title']} ({i['severity']}, {i['count']}×)" for i in ov["open_incidents"]))
    lines.append("What to do next:")
    lines += [f"• {s}" for s in steps]
    return "\n".join(lines)


ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer", "next_steps"],
                 "properties": {"answer": {"type": "string", "maxLength": 3000},
                                "next_steps": {"type": "array", "items": {"type": "string", "maxLength": 300},
                                               "maxItems": 8}}}
SYSTEM = ("You are the Engine Assistant of a trading platform. Answer the operator's question using ONLY the facts in "
          "the JSON you are given (engine state, signal diagnosis, accounts, incidents). If a fact is missing, say it "
          "is not recorded; never guess prices, results or reasons. Explain plainly why a signal was or was not "
          "executed and what the operator should do next, naming the console page or desktop action. You cannot "
          "act: never claim to have changed anything, and never advise disabling safety controls, raising risk "
          "limits to force a trade, enabling LIVE trading, or placing manual orders to bypass the Risk Engine.")


def answer(db: Session, question: str, signal_id: int | None = None, history: list[dict] | None = None,
           use_ai: bool = True, provider_factory=None) -> dict:
    q = (question or "").strip()[:1000]
    m = SIGNAL_RE.search(q)
    sid = signal_id or (int(m.group(1)) if m else None)
    intent = "signal" if sid else "why" if WHY_RE.search(q) else "next" if NEXT_RE.search(q) else "status"
    ov = overview(db)
    steps = next_steps(ov)
    diag = None
    if sid:
        sig = db.get(TradingSignal, sid)
        if sig is None:
            return {"answer": f"There is no signal #{sid}.", "source": "engine", "intent": intent, "next_steps": steps,
                    "diagnosis": None, "facts": {"overview": ov}}
        diag = diagnose_signal(db, sig)
    elif intent == "why":
        sig = latest_valid_signal(db)
        diag = diagnose_signal(db, sig) if sig is not None else None
    text = _engine_text(intent, ov, diag, steps)
    out = {"answer": text, "source": "engine", "intent": intent, "next_steps": steps, "diagnosis": diag,
           "facts": {"overview": ov}}
    if not use_ai:
        return out
    from app.ai import gateway
    if not gateway.configured("engine_assistant"):
        return out
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str
        next_steps: list[str]
    facts = json.loads(json.dumps({"question": q, "overview": ov, "signal_diagnosis": diag, "engine_next_steps": steps,
                                   "recent_conversation": (history or [])[-6:]}, default=str))
    try:
        ai, meta = gateway.run("engine_assistant", SYSTEM, facts, ANSWER_SCHEMA, Answer, max_tokens=1500,
                               provider_factory=provider_factory)
    except gateway.GatewayError as exc:
        out["ai_error"] = str(exc)[:200]
        return out
    out.update(answer=ai.answer, next_steps=ai.next_steps or steps, source="ai",
               ai={"provider": meta["provider"], "model": meta["model"]}, engine_answer=text)
    return out
