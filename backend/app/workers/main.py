"""Trading worker: startup reconciliation, scanning, auto-execution and trade management.

Run as a single process (``python -m app.workers.main``). A Redis lock prevents two workers
from trading the same account concurrently.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import OpenPosition, Symbol, TradingSignal
from app.db.session import session_scope
from app.execution import manager as em
from app.execution.bridge import BridgeClient, BridgeError, BridgeUnavailable
from app.news.service import refresh as refresh_news
from app.risk import engine as risk
from app.scanner.service import expire_signals, scan_once
from app.services import trading as tr
from app.services.events import agent_activity, get_redis, system_event

log = logging.getLogger("worker")
UTC = timezone.utc
LOCK_KEY = "rr:worker:lock"


def startup_checks(bridge: BridgeClient) -> tuple[bool, list[str]]:
    """Part 19: reconnect, verify identity, load real positions/orders, reconcile app state and
    journal, verify SL/TP protection. New entries stay blocked until every check passes."""
    report: list[str] = []
    with session_scope() as db:
        state = tr.get_state(db)
        state.startup_checks_passed = False
        account = tr.active_account(db)
        if account is None:
            report.append("FAIL: no active account configured for mode " + state.active_mode)
            return False, report
        try:
            h = bridge.health()
            report.append(f"MT5 connected={h.get('connected')} identity_ok={h.get('identity_ok')}")
            if not h.get("connected") or not h.get("identity_ok"):
                return False, report + ["FAIL: terminal not connected or wrong account"]
            tr.sync_account(db, bridge, account)
            report.append("account identity & type verified")
            r = tr.sync_positions(db, bridge, account)
            report.append(f"positions synced from MT5: {r}")
            pend = bridge.orders()
            report.append(f"pending orders at broker: {len(pend)}")
            # Parts 27-30 Phase 7 (Part 30.1): compare them with the execution journal, not just count them
            from app.execution.reconcile import compare_pending
            cmp = compare_pending(db, pend, account)
            report.append(f"pending orders compared: {len(cmp['matched'])} matched, "
                          f"{len(cmp['unknown_engine'])} unknown engine order(s), {len(cmp['foreign'])} foreign, "
                          f"{len(cmp['missing_at_broker'])} no longer at the broker")
            if not cmp["ok"]:
                system_event(db, "worker", "critical", "Unknown engine pending orders at the broker",
                             {"orders": cmp["unknown_engine"]})
                return False, report + ["FAIL: the broker holds engine-tagged pending orders the journal does not know; "
                                        "entries blocked until an operator resolves them"]
        except tr.IdentityError as exc:
            em.kill_switch(db, None, None, f"Identity check failed: {exc}")
            return False, report + [f"FAIL: {exc}"]
        except BridgeError as exc:
            return False, report + [f"FAIL: bridge {exc}"]
        rec = em.reconcile_unknown(db, bridge)
        report.append(f"execution journal reconciled: {rec}")
        unresolved = db.scalar(select(em.TradeOrder.id).where(em.TradeOrder.status.in_(["SENT", "UNKNOWN"])).limit(1))
        if unresolved:
            return False, report + ["FAIL: unresolved executions remain; entries blocked until resolved"]
        unprotected = db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id,
                                                            OpenPosition.stop_loss.is_(None))).all()
        if unprotected:
            report.append(f"{len(unprotected)} positions without SL -> applying PROTECT")
            em.apply_position_policy(db, bridge, "startup: unprotected positions", "PROTECT")
            still = db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id,
                                                          OpenPosition.stop_loss.is_(None))).all()
            if still:
                return False, report + ["FAIL: could not protect all positions"]
        state.startup_checks_passed = True
        system_event(db, "worker", "info", "Startup checks passed", {"report": report})
    return True, report


def auto_execute_factory(bridge: BridgeClient):
    def run(signal_id: int) -> None:
        with session_scope() as db:
            state = tr.get_state(db)
            sig = db.get(TradingSignal, signal_id)
            if not state.auto_trading or sig is None or sig.decision not in ("BUY", "SELL") or sig.status != "NEW":
                return
            if state.auto_trading_since is not None and sig.created_at < state.auto_trading_since:
                return  # Phase 15: produced while auto trading was off; never executed retroactively
            review = (sig.ai_review or {}).get("decision")
            if review not in ("BUY", "SELL"):
                return  # only Claude-confirmed candidates are auto-executed
            if review != sig.decision:
                # Phase 15: the review must confirm this direction. The gateway's boundaries already refuse a reversal;
                # the execution path does not rely on that alone.
                log.warning("signal %s: AI review %s does not confirm %s; not executed", signal_id, review, sig.decision)
                return
            sym = db.get(Symbol, sig.symbol_id)
            account = tr.active_account(db)
            if account is None:
                return
            try:
                em.open_position(db, bridge, symbol=sym.broker_symbol, direction=sig.decision,
                                 stop_loss=float(sig.stop_loss), take_profit=float(sig.tp2) if sig.tp2 else None,
                                 source="AUTO", dedupe_key=f"{account.id}:signal:{sig.id}:OPEN", signal=sig,
                                 strategy_codes=sorted({s["strategy"] for s in sig.strategies}),
                                 expires_at=sig.expires_at)
            except em.ExecutionBlocked as exc:
                log.info("auto-execution blocked for signal %s: %s", signal_id, exc.reason)
            except BridgeError as exc:
                log.warning("auto-execution bridge error: %s", exc)
    return run


def manage_positions(bridge: BridgeClient) -> None:
    """Break-even, partial profit, trailing stops; plus Claude-independent invalidation is
    left to explicit stops. All changes go through idempotent journalled commands."""
    with session_scope() as db:
        account = tr.active_account(db)
        if account is None:
            return
        params = tr.risk_params(db, account.mode)
        for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id)).all():
            sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbol))
            if sym is None:
                continue
            spec = tr.spec_for(sym)
            from app.scanner.service import bars_to_df
            from app.analysis.indicators import atr
            try:
                df = bars_to_df(bridge.rates(p.symbol, "H1", 60))
                a = float(atr(df).iloc[-1]) if len(df) > 20 else None
            except BridgeError:
                a = None
            initial = None
            if p.execution_id:
                o = db.scalar(select(em.TradeOrder).where(em.TradeOrder.execution_id == p.execution_id))
                initial = float(o.stop_loss) if o and o.stop_loss else None
            partial_done = bool(db.scalar(select(em.TradeOrder.id).where(
                em.TradeOrder.position_ticket == p.ticket, em.TradeOrder.action == "PARTIAL_CLOSE",
                em.TradeOrder.status == "CONFIRMED").limit(1)))
            view = risk.PositionView(p.ticket, p.symbol, p.direction, float(p.volume), float(p.open_price),
                                     float(p.current_price or p.open_price), float(p.stop_loss) if p.stop_loss else None,
                                     float(p.take_profit) if p.take_profit else None, initial, partial_done)
            # Parts 27-30 Phase 6: optional exits (time, volatility, structure, invalidation); off unless configured
            exits = _exit_actions(db, bridge, p, view, params) if _exits_enabled(params) else []
            for act in exits:
                key = f"{account.id}:exit:{p.ticket}"
                try:
                    em.manage(db, bridge, action="CLOSE", position_ticket=p.ticket, dedupe_key=key)
                    agent_activity("execution", "approved", f"{p.symbol} #{p.ticket}: {act['reason']}")
                    from app.notifications.service import notify
                    notify(db, "TRADE_CLOSED", f"{p.symbol} #{p.ticket} closed", act["reason"])
                except em.ExecutionBlocked as exc:
                    log.info("exit blocked: %s", exc.reason)
            if exits:
                continue
            for act in risk.manage_position(view, params, a, spec):
                key = f"{account.id}:manage:{p.ticket}:{act['action']}:{act.get('stop_loss', act.get('volume'))}"
                try:
                    em.manage(db, bridge, action="MODIFY_SLTP" if act["action"] == "MODIFY" else act["action"],
                              position_ticket=p.ticket, dedupe_key=key, stop_loss=act.get("stop_loss"),
                              volume=act.get("volume"))
                    agent_activity("execution", "approved", f"{p.symbol} #{p.ticket}: {act.get('reason')}")
                    if act.get("reason") == "break-even":
                        from app.notifications.service import notify
                        notify(db, "BREAK_EVEN", f"{p.symbol} #{p.ticket} moved to break-even")
                except em.ExecutionBlocked as exc:
                    log.info("manage blocked: %s", exc.reason)


def _exits_enabled(params) -> bool:
    return bool(params.max_holding_minutes or params.volatility_exit_atr_ratio or params.structure_exit
                or params.invalidation_exit)


def _exit_actions(db, bridge: BridgeClient, p, view, params) -> list[dict]:
    """Inputs for ``risk.exit_actions`` from the position's originating signal (its timeframe, the ATR its analysis
    measured, the engine's latest analysis since the position opened). Missing data means no exit is taken on it."""
    from app.analysis.indicators import atr
    from app.analysis.structure import structure_events
    from app.db.models import AnalysisResult
    from app.scanner.service import bars_to_df
    sig = None
    if p.execution_id:
        o = db.scalar(select(em.TradeOrder).where(em.TradeOrder.execution_id == p.execution_id))
        sig = db.get(TradingSignal, o.signal_id) if o is not None and o.signal_id else None
    tf = sig.timeframe if sig is not None else "H1"
    ar = db.get(AnalysisResult, sig.analysis_id) if sig is not None and sig.analysis_id else None
    atr_entry = (ar.structure or {}).get("atr") if ar is not None else None
    atr_now, choch = None, False
    try:
        df = bars_to_df(bridge.rates(p.symbol, tf, 120))
        if len(df) > 30:
            atr_now = float(atr(df).iloc[-1])
            sgn = 1 if p.direction == "BUY" else -1
            events, _ = structure_events(df)
            opened = p.opened_at
            choch = any(e.kind == "CHOCH" and (1 if e.direction == "BUY" else -1) != sgn and
                        df.index[e.idx].to_pydatetime() >= opened for e in events) if opened else False
    except BridgeError:
        pass
    view_now = None
    if sig is not None and p.opened_at is not None:
        latest = db.scalar(select(AnalysisResult).where(AnalysisResult.symbol_id == sig.symbol_id,
                                                        AnalysisResult.timeframe == tf,
                                                        AnalysisResult.created_at > p.opened_at)
                           .order_by(AnalysisResult.id.desc()).limit(1))
        view_now = (latest.score or {}).get("decision") if latest is not None else None
    return risk.exit_actions(view, params, opened_at=p.opened_at, now=datetime.now(UTC), atr_now=atr_now,
                             atr_entry=float(atr_entry) if atr_entry else None, choch_against=choch,
                             engine_view=view_now)


def heartbeat(bridge: BridgeClient, last_ok: list[bool]) -> bool:
    try:
        h = bridge.health()
        ok = bool(h.get("connected"))
        get_redis().set("rr:bridge:health", __import__("json").dumps({**h, "ts": datetime.now(UTC).isoformat()}), ex=60)
    except (BridgeError, Exception) as exc:  # noqa: BLE001
        ok = False
        try:
            get_redis().set("rr:bridge:health", __import__("json").dumps({"connected": False, "error": str(exc),
                                                                         "ts": datetime.now(UTC).isoformat()}), ex=60)
        except Exception:  # noqa: BLE001
            pass
    if ok != last_ok[0]:
        with session_scope() as db:
            tr.notify_mt5(db, ok)
            if not ok:
                tr.get_state(db).startup_checks_passed = False  # must re-run reconciliation after reconnect
        last_ok[0] = ok
    return ok


def main() -> None:
    configure_logging()
    s = get_settings()
    r = get_redis()
    if not r.set(LOCK_KEY, "1", nx=True, ex=120):
        log.error("another worker holds the trading lock; exiting")
        return
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    bridge = BridgeClient()
    agent = None
    if s.anthropic_api_key:
        from app.agents.claude_agent import ClaudeAgent
        agent = ClaudeAgent()
    auto = auto_execute_factory(bridge)
    last_ok = [True]
    last_scan = last_news = 0.0
    while not stop["flag"]:
        r.expire(LOCK_KEY, 120)
        ok = heartbeat(bridge, last_ok)
        if ok:
            with session_scope() as db:
                passed = tr.get_state(db).startup_checks_passed
            if not passed:
                good, report = startup_checks(bridge)
                log.info("startup checks: %s %s", good, report)
            try:
                with session_scope() as db:
                    acct = tr.active_account(db)
                    if acct:
                        tr.sync_account(db, bridge, acct)
                        tr.sync_positions(db, bridge, acct)
                    em.reconcile_unknown(db, bridge)
                    expire_signals(db)
                manage_positions(bridge)
                if time.time() - last_news > 3600:
                    with session_scope() as db:
                        refresh_news(db)
                    last_news = time.time()
                if time.time() - last_scan > s.scan_interval_seconds:
                    log.info("scan: %s", scan_once(bridge, agent, auto))
                    last_scan = time.time()
            except tr.IdentityError as exc:
                with session_scope() as db:
                    em.kill_switch(db, bridge, None, f"Account identity changed: {exc}")
            except BridgeUnavailable as exc:
                log.warning("bridge unavailable: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("worker cycle failed")
        time.sleep(s.bridge_heartbeat_seconds)
    r.delete(LOCK_KEY)


if __name__ == "__main__":
    main()
