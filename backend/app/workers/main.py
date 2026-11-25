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
from app.db.models import OpenPosition, Symbol, TradingAccount, TradingSignal
from app.db.session import session_scope
from app.execution import manager as em
from app.execution.bridge import AccountBridges, BridgeClient, BridgeError, BridgeUnavailable
from app.news.service import refresh as refresh_news
from app.risk import engine as risk
from app.scanner.service import expire_signals, scan_once
from app.services import trading as tr
from app.services.events import agent_activity, get_redis, system_event

log = logging.getLogger("worker")
UTC = timezone.utc
LOCK_KEY = "rr:worker:lock"


def startup_checks(bridge: BridgeClient, account_id: int | None = None) -> tuple[bool, list[str]]:
    """Part 19: reconnect, verify identity, load real positions/orders, reconcile app state and
    journal, verify SL/TP protection. New entries stay blocked until every check passes.
    Multi-account: ``account_id`` reconciles that execution account through its own connector (default: the active
    account, unchanged)."""
    report: list[str] = []
    with session_scope() as db:
        state = tr.get_state(db)
        account = db.get(TradingAccount, account_id) if account_id is not None else tr.active_account(db)
        if account is None:
            if account_id is None:
                state.startup_checks_passed = False
            report.append("FAIL: no active account configured for mode " + state.active_mode)
            return False, report
        tr.set_reconciled(db, account, False)
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
        rec = em.reconcile_unknown(db, bridge, None if account.is_active else account)
        report.append(f"execution journal reconciled: {rec}")
        uq = select(em.TradeOrder.id).where(em.TradeOrder.status.in_(["SENT", "UNKNOWN"]))
        if not account.is_active:
            uq = uq.where(em.TradeOrder.account_id == account.id)
        unresolved = db.scalar(uq.limit(1))
        if unresolved:
            return False, report + ["FAIL: unresolved executions remain; entries blocked until resolved"]
        unprotected = db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id,
                                                            OpenPosition.stop_loss.is_(None))).all()
        if unprotected:
            report.append(f"{len(unprotected)} positions without SL -> applying PROTECT")
            em.apply_position_policy(db, bridge, "startup: unprotected positions", "PROTECT", account=account)
            still = db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id,
                                                          OpenPosition.stop_loss.is_(None))).all()
            if still:
                return False, report + ["FAIL: could not protect all positions"]
        tr.set_reconciled(db, account, True)
        system_event(db, "worker", "info", "Startup checks passed", {"report": report, "account": account.login})
    return True, report


def auto_execute_factory(bridge: BridgeClient, bridges: AccountBridges | None = None):
    """Multi-account: one signal is evaluated for every execution account (the active account first, then every other
    account enabled for trading in the active mode). Each account passes its own eligibility, Risk Engine (its own
    limits and equity) and idempotency key, through its own connector."""
    bridges = bridges or AccountBridges(bridge, factory=lambda a: None)

    def run(signal_id: int) -> None:
        with session_scope() as db:
            state = tr.get_state(db)
            sig = db.get(TradingSignal, signal_id)
            if sig is None or sig.status != "NEW":
                return
            # Phase 15 rules plus the operator's AI mode: in AI mode only candidates the AI review confirmed in the same
            # direction; in engine-only mode the engine's own BUY/SELL produced without AI. Never retroactively.
            why = tr.auto_execution_block(state, sig)
            if why is not None:
                if state.auto_trading and sig.decision in ("BUY", "SELL"):
                    log.info("signal %s not auto-executed: %s", signal_id, why)
                return
            sym = db.get(Symbol, sig.symbol_id)
            codes = sorted({s["strategy"] for s in sig.strategies})
            for account in tr.execution_accounts(db):
                blocked = tr.account_block(account, sym.broker_symbol, codes)
                if blocked is not None:
                    log.info("signal %s not auto-executed on account %s: %s", signal_id, account.login, blocked)
                    continue
                b = bridges.get(account)
                if b is None:
                    log.info("signal %s: no connector for account %s (%s)", signal_id, account.login,
                             bridges.errors.get(account.id, "not connected"))
                    continue
                try:
                    em.open_position(db, b, symbol=sym.broker_symbol, direction=sig.decision,
                                     stop_loss=float(sig.stop_loss), take_profit=float(sig.tp2) if sig.tp2 else None,
                                     source="AUTO", dedupe_key=f"{account.id}:signal:{sig.id}:OPEN", signal=sig,
                                     strategy_codes=codes, expires_at=sig.expires_at, account=account)
                except em.ExecutionBlocked as exc:
                    log.info("auto-execution blocked for signal %s on account %s: %s", signal_id, account.login,
                             exc.reason)
                except BridgeError as exc:
                    log.warning("auto-execution bridge error on account %s: %s", account.login, exc)
    return run


def manage_positions(bridge: BridgeClient, account_id: int | None = None) -> None:
    """Break-even, partial profit, trailing stops; plus Claude-independent invalidation is
    left to explicit stops. All changes go through idempotent journalled commands. ``account_id``: one execution
    account through its own connector (default: the active account)."""
    with session_scope() as db:
        account = db.get(TradingAccount, account_id) if account_id is not None else tr.active_account(db)
        if account is None:
            return
        params = tr.account_risk_params(db, account)
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
                    em.manage(db, bridge, action="CLOSE", position_ticket=p.ticket, dedupe_key=key, account=account)
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
                    em.manage(db, bridge, account=account,
                              action="MODIFY_SLTP" if act["action"] == "MODIFY" else act["action"],
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


def other_accounts_cycle(bridges: AccountBridges) -> dict:
    """Multi-account: maintain every execution account other than the active one (the active account keeps its
    original path). Per account, through its own connector: health, startup reconciliation, account and position sync,
    journal reconciliation, the kill switch's position policy, and position management. One account's failure never
    affects another; a disconnected account is reconciled again before its next entry."""
    out: dict = {}
    with session_scope() as db:
        st = tr.get_state(db)
        ids = [a.id for a in tr.execution_accounts(db) if not a.is_active]
        if not st.kill_switch:          # the kill switch was reset: the policy applies again on the next one
            for a in db.scalars(select(TradingAccount).where(TradingAccount.emergency_applied.is_(True))):
                a.emergency_applied = False
    for aid in ids:
        with session_scope() as db:
            acct = db.get(TradingAccount, aid)
            login = acct.login
            bridge = bridges.get(acct)
        if bridge is None:
            out[login] = "no connector"
            continue
        try:
            h = bridge.health()
            if not h.get("connected") or not h.get("identity_ok"):
                with session_scope() as db:
                    tr.set_reconciled(db, db.get(TradingAccount, aid), False)
                out[login] = "disconnected"
                continue
            with session_scope() as db:
                ok = tr.is_reconciled(db, db.get(TradingAccount, aid))
            if not ok:
                good, report = startup_checks(bridge, aid)
                log.info("startup checks for account %s: %s %s", login, good, report)
                if not good:
                    out[login] = "reconciliation failed"
                    continue
            with session_scope() as db:
                acct = db.get(TradingAccount, aid)
                tr.sync_account(db, bridge, acct)
                tr.sync_positions(db, bridge, acct)
                em.reconcile_unknown(db, bridge, acct)
                st = tr.get_state(db)
                if st.kill_switch and not acct.emergency_applied:
                    reason = st.kill_reason or "kill switch"
                    em.apply_position_policy(db, bridge, reason, account=acct)
                    from app.execution.reconcile import cancel_engine_pending
                    cancel_engine_pending(db, bridge, acct, reason, None)
                    acct.emergency_applied = True
            manage_positions(bridge, aid)
            out[login] = "ok"
        except tr.IdentityError as exc:
            with session_scope() as db:
                em.kill_switch(db, None, None, f"Account {login} identity changed: {exc}")
            out[login] = "identity changed"
        except BridgeError as exc:
            with session_scope() as db:
                tr.set_reconciled(db, db.get(TradingAccount, aid), False)
            bridges.drop(aid)
            out[login] = f"bridge error: {exc}"
        except Exception:  # noqa: BLE001 - one account's failure must not stop the others
            log.exception("account %s cycle failed", login)
            out[login] = "error"
    return out


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
    bridges = AccountBridges(bridge)
    auto = auto_execute_factory(bridge, bridges)
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
                other_accounts_cycle(bridges)
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
