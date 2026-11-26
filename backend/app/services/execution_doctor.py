"""Why are trades not executing? One answer from the engine's own records (no AI call).

1. **Pipeline gates**, in the order a signal meets them: trading worker running, auto trading, kill switch, risk lock,
   paused entries, AI review mode, execution account and its reconciliation, the news calendar.
2. **The last 24 hours**: BUY/SELL signals, how many were executed, and for the others the reason the trading worker
   gave (the same rule it uses) and the Risk Engine's actual rejection checks, counted.

Every blocker comes with the fix. Nothing is changed here: risk controls are never relaxed by the diagnosis.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

UTC = timezone.utc

RISK_FIX = {
    "news": "The news calendar is not available, so news risk is UNKNOWN and entries are refused. Check internet "
            "access for the engine (the built-in calendar refreshes hourly), or configure a calendar. Only if you "
            "accept trading without news protection in DEMO: Risk settings → DEMO → allow trading when news risk is "
            "unknown.",
    "spread": "The spread is wider than the allowed maximum. Trade at liquid hours, or review the maximum spread in "
              "Risk settings (per account: Settings → account → Risk limits).",
    "min_rr": "Reward:risk below the minimum. This is the strategy's setup; it is refused by design.",
    "signal_expiry": "The signal expired before execution. Keep the trading worker running so signals are executed "
                     "as they appear.",
    "max_open_positions": "The maximum number of open positions is reached.",
    "max_trades_per_symbol": "A position is already open on this symbol (per-symbol limit).",
    "duplicate_position": "The same position is already open.",
    "daily_profit_target": "The daily profit target is reached; new entries pause until tomorrow.",
    "max_drawdown": "The drawdown limit is reached.", "floating_loss": "The floating-loss limit is reached.",
    "consecutive_losses": "The consecutive-loss limit is reached.",
    "free_margin": "Not enough free margin on the account.",
    "volume_min": "The risk-based size is below the broker's minimum lot: the stop is too wide for the risk per trade "
                  "on this balance. Review risk per trade in Risk settings or the symbol.",
    "kill_switch": "The kill switch is on (Control Center → reset after checking why).",
    "risk_lock": "A risk limit locked trading (Control Center).",
    "pause_new_entries": "New entries are paused (Control Center).",
    "symbol_tradable": "The broker disabled trading for this symbol (market closed or symbol restricted).",
    "market_anomaly": "A market anomaly was detected on this symbol (abnormal spread or gap); entries wait.",
    "correlated_exposure": "Too much exposure to correlated symbols.",
    "total_exposure": "The total exposure limit is reached.", "leverage_exposure": "The leverage exposure limit is reached.",
    "strategy_concentration": "Too many positions from one strategy.",
    "stops_level": "The stop is closer than the broker allows.",
    "slippage": "Expected slippage is above the limit.",
}

AUTO_FIX = [
    ("auto trading is OFF", "Control Center → switch Auto trading ON."),
    ("produced before auto trading", "Only signals created after Auto trading was switched on are executed; wait "
                                     "for the next signals."),
    ("not confirmed by the AI", "AI Studio → Engine autonomy → AI review of scan candidates: Off lets the engine "
                                "decide (the Risk Engine still checks every order)."),
    ("AI review", "AI Studio → Engine autonomy → AI review of scan candidates: Off (the engine decides)."),
    ("AI was unavailable", "AI Studio → Engine autonomy: set AI review to Off, or Budget → if AI is unavailable: "
                           "continue without AI."),
    ("engine-only mode executes only", "Signals produced before switching to Run without AI are not executed; wait "
                                       "for new signals."),
    ("WAIT / NO_TRADE", "Not a trade signal."),
]


def _fix_for_block(reason: str) -> str:
    return next((f for k, f in AUTO_FIX if k.lower() in reason.lower()), "See the signal in the Signal Centre (Ask why).")


def gates(db: Session) -> list[dict]:
    from app.news.service import provider_healthy
    from app.services import trading as tr
    from app.services.events import get_redis
    st = tr.get_state(db)
    out = []

    def gate(name: str, ok: bool, detail: str, fix: str) -> None:
        out.append({"gate": name, "ok": bool(ok), "detail": detail, "fix": None if ok else fix})
    try:
        worker = bool(get_redis().exists("rr:role:trading:lock"))
    except Exception:  # noqa: BLE001
        worker = False
    gate("Trading worker running", worker, "running" if worker else "stopped",
         "Desktop app → Start Trading Worker (it is stopped by default and only executes signals created after it "
         "starts).")
    gate("Auto trading ON", st.auto_trading, "on" if st.auto_trading else "off", "Control Center → Auto trading ON.")
    gate("Kill switch clear", not st.kill_switch, st.kill_reason or "clear", "Control Center → check the reason, then "
         "reset the kill switch.")
    gate("Risk lock clear", not st.risk_lock, st.risk_lock_reason or "clear", "Control Center → review the limit that "
         "tripped; it clears per its rule (e.g. next day) or by an administrator.")
    gate("New entries not paused", not st.pause_new_entries, "paused" if st.pause_new_entries else "accepting",
         "Control Center → resume new entries.")
    gate("Startup checks passed", st.startup_checks_passed, "passed" if st.startup_checks_passed else "not passed",
         "Start the MT5 connector and the engine (desktop app → Start All); the monitor runs the checks.")
    acct = tr.active_account(db)
    gate("Execution account", acct is not None and (acct.trading_enabled or acct.is_active),
         f"{acct.login} {acct.mode}" if acct else "none",
         "Settings → Accounts: add/verify the account and enable trading.")
    if acct is not None:
        rec = tr.is_reconciled(db, acct)
        gate("Account reconciled", rec, "yes" if rec else "no",
             "Keep the MT5 connector running; reconciliation runs automatically after the connection returns.")
    pol = tr.review_policy(db)["candidate_review"]
    ai_ok = st.ai_mode == "ENGINE_ONLY" or pol == "off" or tr.ai_unavailable_policy(db) == "engine_only"
    gate("Candidates do not wait for the AI", ai_ok,
         f"AI mode {st.ai_mode}, candidate review {pol}, if AI unavailable: {tr.ai_unavailable_policy(db)}",
         "AI Studio → Engine autonomy → AI review of scan candidates: Off.")
    news = provider_healthy()
    params = tr.account_risk_params(db, acct) if acct is not None else None
    blocks = bool(params and params.news_unknown_blocks)
    gate("News calendar available", news or not blocks,
         "calendar up to date" if news else ("calendar unavailable: news risk UNKNOWN, entries refused" if blocks
                                             else "calendar unavailable (allowed by your risk settings)"),
         RISK_FIX["news"])
    return out


def last_day(db: Session) -> dict:
    from app.db.models import RiskEvaluation, TradeOrder, TradingSignal
    from app.services import trading as tr
    since = datetime.now(UTC) - timedelta(hours=24)
    st = tr.get_state(db)
    policy = tr.ai_unavailable_policy(db)
    sigs = db.scalars(select(TradingSignal).where(TradingSignal.created_at >= since,
                                                  TradingSignal.decision.in_(["BUY", "SELL"]))).all()
    executed = {o.signal_id for o in db.scalars(select(TradeOrder).where(TradeOrder.created_at >= since,
                                                                         TradeOrder.status == "CONFIRMED",
                                                                         TradeOrder.signal_id.isnot(None)))}
    blocks = Counter()
    for s in sigs:
        if s.id in executed:
            continue
        why = tr.auto_execution_block(st, s, policy)
        if why:
            blocks[why.split(":")[0][:120]] += 1
    checks, examples = Counter(), {}
    for ev in db.scalars(select(RiskEvaluation).where(RiskEvaluation.created_at >= since,
                                                      RiskEvaluation.approved.is_(False))):
        for c in ev.checks or []:
            if not c.get("passed"):
                checks[c["name"]] += 1
                examples.setdefault(c["name"], c.get("detail", ""))
    return {"signals": len(sigs), "executed": len(executed & {s.id for s in sigs}),
            "not_executed_reasons": [{"reason": r, "count": n, "fix": _fix_for_block(r)}
                                     for r, n in blocks.most_common(8)],
            "risk_rejections": [{"check": k, "count": n, "example": examples.get(k, ""),
                                 "fix": RISK_FIX.get(k, "See Risk settings for this check.")}
                                for k, n in checks.most_common(10)]}


def diagnose(db: Session) -> dict:
    g = gates(db)
    day = last_day(db)
    failing = [x for x in g if not x["ok"]]
    if failing:
        verdict = f"Trades cannot execute: {failing[0]['gate'].lower()} — {failing[0]['detail']}."
        next_step = failing[0]["fix"]
    elif day["risk_rejections"]:
        top = day["risk_rejections"][0]
        verdict = f"The pipeline is open, but the Risk Engine refused entries; most often: {top['check']} ({top['count']}x)."
        next_step = top["fix"]
    elif day["signals"] == 0:
        verdict = "The pipeline is open; no BUY/SELL signal in the last 24 hours (no valid opportunity was found)."
        next_step = ("Nothing is forced. Check Market Scanner (symbols scanned, data) and Broker Symbols (enabled "
                     "symbols); more symbols and timeframes give more opportunities.")
    else:
        verdict = f"The pipeline is open: {day['executed']} of {day['signals']} signals executed in 24 hours."
        next_step = "No blocker found."
    return {"gates": g, "last_24h": day, "verdict": verdict, "next_step": next_step,
            "can_execute": not failing}
