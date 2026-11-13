"""Trade Execution Manager.

Order of operations (spec 10.3) for a new position:
  expiry -> mode -> MT5 connection -> account identity/server -> symbol & market -> live spec ->
  margin quote -> Risk Engine (sizing, SL/TP, spread, slippage, exposure, duplicates) ->
  idempotency reservation -> submit -> MT5 confirmation -> persist -> dashboard update.

An order is CONFIRMED only when the connector returns an MT5 success retcode. Network
failures leave the order in UNKNOWN and it is resolved exclusively by journal lookup
(``reconcile_unknown``) — never by blind resubmission with a new id.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (ExecutionIdempotency, OpenPosition, RiskEvaluation, Symbol, TradeExecution, TradeOrder,
                           TradingAccount, TradingSignal)
from app.execution.bridge import BridgeClient, BridgeRejected, BridgeUnavailable
from app.notifications.service import notify
from app.risk import engine as risk
from app.services import trading as tr
from app.services.events import agent_activity, audit, publish, system_event

log = logging.getLogger("execution")
UTC = timezone.utc
ENTRY_ACTIONS = {"MARKET_BUY", "MARKET_SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}


def mt5_tag(execution_id: str) -> str:
    return "RR" + hashlib.sha1(execution_id.encode()).hexdigest()[:14]


class ExecutionBlocked(Exception):
    def __init__(self, reason: str, evaluation_id: int | None = None):
        super().__init__(reason)
        self.reason, self.evaluation_id = reason, evaluation_id


def _reserve(db: Session, dedupe_key: str) -> tuple[str, TradeOrder | None]:
    """Reserve an execution id for this dedupe key. Returns (execution_id, existing_order_or_None)."""
    existing = db.scalar(select(ExecutionIdempotency).where(ExecutionIdempotency.dedupe_key == dedupe_key))
    if existing:
        return existing.execution_id, db.scalar(select(TradeOrder).where(TradeOrder.execution_id == existing.execution_id))
    eid = uuid.uuid4().hex
    try:
        with db.begin_nested():
            db.add(ExecutionIdempotency(execution_id=eid, dedupe_key=dedupe_key, state="RESERVED"))
    except IntegrityError:  # concurrent reservation won the race
        row = db.scalar(select(ExecutionIdempotency).where(ExecutionIdempotency.dedupe_key == dedupe_key))
        return row.execution_id, db.scalar(select(TradeOrder).where(TradeOrder.execution_id == row.execution_id))
    return eid, None


def _apply_result(db: Session, order: TradeOrder, res: dict) -> TradeOrder:
    idem = db.get(ExecutionIdempotency, order.execution_id)
    state = res.get("state")
    if state == "DONE":
        order.status, order.status_detail = "CONFIRMED", res.get("retcode_text") or "MT5 confirmed"
        if not db.scalar(select(TradeExecution.id).where(TradeExecution.order_id == order.id)):
            db.add(TradeExecution(order_id=order.id, retcode=int(res.get("retcode") or 0),
                                  retcode_text=str(res.get("retcode_text") or ""), mt5_order=res.get("order"),
                                  mt5_deal=res.get("deal"),
                                  filled_volume=Decimal(str(res["volume"])) if res.get("volume") else None,
                                  filled_price=Decimal(str(res["price"])) if res.get("price") else None,
                                  slippage_points=res.get("slippage_points"), latency_ms=res.get("latency_ms"),
                                  raw={k: v for k, v in res.items() if k != "request"}))
        if idem:
            idem.state = "DONE"
        notify(db, "TRADE_EXECUTED", f"{order.action} {order.symbol} {order.volume} confirmed",
               f"price {res.get('price')} ticket {res.get('order')}", {"execution_id": order.execution_id})
    elif state in ("REJECTED", "FAILED"):
        order.status = "REJECTED" if state == "REJECTED" else "FAILED"
        if res.get("retcode") and not db.scalar(select(TradeExecution.id).where(TradeExecution.order_id == order.id)):
            # MT5's rejection answer (e.g. requote 10004) is recorded for execution analytics; it is not a fill.
            db.add(TradeExecution(order_id=order.id, retcode=int(res["retcode"]), retcode_text=str(res.get("retcode_text") or ""),
                                  latency_ms=res.get("latency_ms"), raw={k: v for k, v in res.items() if k != "request"}))
        order.status_detail = str(res.get("reason") or res.get("retcode_text") or state)
        if idem:
            idem.state = "FAILED"
        notify(db, "EXECUTION_FAILURE" if state == "FAILED" else "TRADE_REJECTED",
               f"{order.action} {order.symbol} {state.lower()}", order.status_detail, {"execution_id": order.execution_id})
    else:  # UNKNOWN / NOT_FOUND-in-flight
        order.status, order.status_detail = "UNKNOWN", str(res.get("reason") or "awaiting MT5 confirmation")
        if idem:
            idem.state = "UNKNOWN"
    publish("order", {"execution_id": order.execution_id, "status": order.status, "symbol": order.symbol,
                      "action": order.action, "detail": order.status_detail})
    return order


def _send(db: Session, bridge: BridgeClient, order: TradeOrder, account: TradingAccount, extra: dict) -> TradeOrder:
    cmd = {"execution_id": order.execution_id, "action": order.action, "symbol": order.symbol,
           "volume": float(order.volume), "account_login": account.login, "account_server": account.server,
           "deviation_points": order.deviation_points, **extra}
    if order.price is not None:
        cmd["price"] = float(order.price)
    if order.stop_loss is not None:
        cmd["stop_loss"] = float(order.stop_loss)
    if order.take_profit is not None:
        cmd["take_profit"] = float(order.take_profit)
    if order.position_ticket:
        cmd["position_ticket"] = int(order.position_ticket)
    order.status = "SENT"
    idem = db.get(ExecutionIdempotency, order.execution_id)
    if idem:
        idem.state = "SENT"
    db.commit()  # persist intent before the network call
    try:
        res = bridge.submit(cmd)
    except BridgeUnavailable as exc:
        res = {"state": "UNKNOWN", "reason": f"bridge unavailable: {exc}"}
        system_event(db, "execution", "error", f"Outcome unknown for {order.execution_id}; reconciliation required")
    except BridgeRejected as exc:
        res = {"state": "REJECTED", "reason": f"connector rejected: {exc.detail}"}
    _apply_result(db, order, res)
    db.commit()
    return order


def open_position(db: Session, bridge: BridgeClient, *, symbol: str, direction: str, stop_loss: float | None,
                  take_profit: float | None, source: str, dedupe_key: str, user_id: int | None = None,
                  signal: TradingSignal | None = None, order_type: str = "MARKET", price: float | None = None,
                  requested_volume: float | None = None, size_multiplier: float = 1.0,
                  strategy_codes: list[str] | None = None, expires_at: datetime | None = None,
                  confirm_live: bool = False) -> TradeOrder:
    state = tr.get_state(db)
    account = tr.active_account(db)
    if account is None:
        raise ExecutionBlocked("No active trading account for the current mode")
    if account.mode != state.active_mode:
        raise ExecutionBlocked("Active account mode does not match trading mode")
    if account.mode == "LIVE" and (not account.live_approved_by or not account.verified):
        raise ExecutionBlocked("LIVE account not verified/approved by an administrator")
    if account.mode == "LIVE" and source == "MANUAL" and not confirm_live:
        raise ExecutionBlocked("Explicit LIVE confirmation required for manual orders")
    if source == "AUTO" and not state.auto_trading:
        raise ExecutionBlocked("Auto trading is OFF")
    if not state.startup_checks_passed:
        raise ExecutionBlocked("Startup reconciliation has not passed; new entries blocked")

    # Idempotency first: the same signal can never produce two orders.
    eid, existing = _reserve(db, dedupe_key)
    if existing is not None:
        return existing

    # Live checks against the terminal (raises on connection / identity problems)
    health = bridge.health()
    if not health.get("connected") or not health.get("identity_ok"):
        raise ExecutionBlocked(f"MT5 not ready: connected={health.get('connected')} identity={health.get('identity_ok')}")
    snap = tr.sync_account(db, bridge, account)
    sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == symbol))
    if sym is None:
        raise ExecutionBlocked(f"Unknown broker symbol {symbol} (run symbol discovery)")
    live_spec = bridge.symbol(symbol)
    spec = tr.spec_for(sym, live_spec)
    tick = bridge.tick(symbol)
    if not tick.get("bid") or not tick.get("ask"):
        raise ExecutionBlocked("No live quote; market closed or unavailable")
    if tick.get("time") and datetime.now(UTC).timestamp() - float(tick["time"]) > _stale_limit():
        raise ExecutionBlocked(f"Stale quote ({datetime.now(UTC).timestamp() - float(tick['time']):.0f}s old) cannot authorise an entry")
    entry = float(tick["ask"] if direction == "BUY" else tick["bid"]) if order_type == "MARKET" else float(price or 0)
    spread_pts = round((float(tick["ask"]) - float(tick["bid"])) / spec.point, 1)   # avoid float noise (30.0000001 > 30)
    margin = bridge.margin(symbol, direction, 1.0, entry).get("margin")
    params = tr.risk_params(db, account.mode)
    from app.intelligence import anomaly as anomaly_intel
    from app.intelligence import portfolio as portfolio_intel
    control = risk.ControlState(state.kill_switch, state.risk_lock, state.pause_new_entries,
                                _news_status(db, symbol, sym), anomaly_block=anomaly_intel.active_block(db, symbol))
    req = risk.OrderRequest(symbol, direction, entry, stop_loss, take_profit, spread_pts,
                            expected_slippage_points=params.max_slippage_points / 2, size_multiplier=size_multiplier,
                            expires_at=expires_at, source=source, strategy_codes=strategy_codes or [],
                            requested_volume=requested_volume, margin_required_per_lot=margin)
    decision = risk.evaluate(req, spec, tr.account_state(db, account, snap), tr.open_risks(db, account), params, control,
                             corr=portfolio_intel.cached_corr())
    ev = RiskEvaluation(signal_id=signal.id if signal else None, account_id=account.id, approved=decision.approved,
                        volume=Decimal(str(decision.volume)), risk_amount=decision.risk_amount,
                        risk_percent=decision.risk_pct, checks=[c.__dict__ for c in decision.checks],
                        reasons=decision.reasons)
    db.add(ev)
    db.flush()
    agent_activity("risk_engine", "approved" if decision.approved else "rejected",
                   f"{direction} {symbol}: " + ("approved " + str(decision.volume) + " lots" if decision.approved
                                                else "; ".join(decision.reasons[:3])))
    if decision.lock_trading and not state.risk_lock:
        trip_risk_lock(db, bridge, decision.lock_reason)
    if not decision.approved:
        idem = db.get(ExecutionIdempotency, eid)
        db.delete(idem)  # rejected before sending — release so a later re-evaluation is possible
        if signal:
            signal.status = "REJECTED"
        notify(db, "RISK_REJECTION", f"Risk Engine rejected {direction} {symbol}", "; ".join(decision.reasons),
               {"risk_evaluation_id": ev.id})
        db.commit()
        raise ExecutionBlocked("; ".join(decision.reasons), ev.id)

    action = f"MARKET_{direction}" if order_type == "MARKET" else f"{direction}_{order_type}"
    order = TradeOrder(execution_id=eid, account_id=account.id, signal_id=signal.id if signal else None,
                       risk_evaluation_id=ev.id, symbol=symbol, action=action, volume=Decimal(str(decision.volume)),
                       price=Decimal(str(price)) if order_type != "MARKET" and price else None,
                       stop_loss=Decimal(str(stop_loss)), take_profit=Decimal(str(take_profit)) if take_profit else None,
                       deviation_points=int(params.max_slippage_points), source=source, requested_by=user_id,
                       strategy_codes=strategy_codes or [])
    db.add(order)
    db.flush()
    audit(db, user_id, "ORDER_SUBMIT", symbol, {"execution_id": eid, "mode": account.mode, "volume": decision.volume,
                                                "source": source})
    _send(db, bridge, order, account, {})
    if signal and order.status == "CONFIRMED":
        signal.status = "EXECUTED"
    if order.status == "CONFIRMED":
        tr.sync_positions(db, bridge, account)
    db.commit()
    agent_activity("execution", "approved" if order.status == "CONFIRMED" else "error",
                   f"{action} {symbol} {order.volume}: {order.status}")
    return order


def manage(db: Session, bridge: BridgeClient, *, action: str, position_ticket: int, dedupe_key: str,
           user_id: int | None = None, stop_loss: float | None = None, take_profit: float | None = None,
           volume: float | None = None, source: str = "SYSTEM") -> TradeOrder:
    """Protective / management commands (modify SL/TP, partial or full close). These reduce risk,
    so they are allowed during kill switch / risk lock, but still journalled and idempotent."""
    account = tr.active_account(db)
    if account is None:
        raise ExecutionBlocked("No active account")
    pos = db.scalar(select(OpenPosition).where(OpenPosition.account_id == account.id,
                                               OpenPosition.ticket == position_ticket))
    if pos is None:
        raise ExecutionBlocked(f"Position {position_ticket} not found (sync positions)")
    if action == "MODIFY_SLTP" and stop_loss is not None:
        sgn = 1 if pos.direction == "BUY" else -1
        reason = None
        if (float(pos.current_price or pos.open_price) - stop_loss) * sgn <= 0:
            reason = "Stop loss would be beyond current price"
        elif pos.stop_loss is not None and (stop_loss - float(pos.stop_loss)) * sgn < 0:
            reason = (f"Refused: moving the stop from {float(pos.stop_loss)} to {stop_loss} widens risk "
                      "(stops are never moved further away to avoid realising a loss)")
        _log_adjustment(db, account.id, position_ticket, "stop_loss", float(pos.stop_loss) if pos.stop_loss else None,
                        stop_loss, reason or f"{source} adjustment", reason is None)
        if reason:
            db.commit()
            raise ExecutionBlocked(reason)
    if action == "MODIFY_SLTP" and take_profit is not None:
        _log_adjustment(db, account.id, position_ticket, "take_profit", float(pos.take_profit) if pos.take_profit else None,
                        take_profit, f"{source} adjustment", True)
    eid, existing = _reserve(db, dedupe_key)
    if existing is not None:
        return existing
    order = TradeOrder(execution_id=eid, account_id=account.id, symbol=pos.symbol, action=action,
                       volume=Decimal(str(volume if volume is not None else pos.volume)), position_ticket=position_ticket,
                       stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                       take_profit=Decimal(str(take_profit)) if take_profit else None, source=source, requested_by=user_id)
    db.add(order)
    db.flush()
    audit(db, user_id, f"POSITION_{action}", pos.symbol, {"ticket": position_ticket, "execution_id": eid})
    _send(db, bridge, order, account, {})
    if order.status == "CONFIRMED":
        tr.sync_positions(db, bridge, account)
        db.commit()
    return order


def reconcile_unknown(db: Session, bridge: BridgeClient) -> dict:
    """Resolve every SENT/UNKNOWN order via the connector journal. Never resubmits with a new id."""
    resolved = 0
    pending = db.scalars(select(TradeOrder).where(TradeOrder.status.in_(["SENT", "UNKNOWN", "PENDING"]))).all()
    for o in pending:
        try:
            res = bridge.execution(o.execution_id)
        except BridgeUnavailable:
            break
        if res.get("state") == "NOT_FOUND":
            if o.status == "PENDING":
                o.status, o.status_detail = "FAILED", "never sent (application stopped before submission)"
                resolved += 1
                continue
            # SENT but connector never journalled it => the request never arrived. Safe to mark failed.
            o.status, o.status_detail = "FAILED", "connector has no record; request never arrived"
            idem = db.get(ExecutionIdempotency, o.execution_id)
            if idem:
                idem.state = "FAILED"
            resolved += 1
            continue
        before = o.status
        _apply_result(db, o, res)
        resolved += int(o.status != before)
    db.commit()
    return {"checked": len(pending), "resolved": resolved}


def trip_risk_lock(db: Session, bridge: BridgeClient | None, reason: str) -> None:
    state = tr.get_state(db)
    state.risk_lock, state.risk_lock_reason = True, reason
    system_event(db, "risk", "critical", f"Risk lock: {reason}")
    notify(db, "DAILY_LOSS_LIMIT" if "daily" in reason else "DRAWDOWN_WARNING", "Trading locked by Risk Engine", reason)
    if bridge is not None:
        apply_position_policy(db, bridge, reason)


def apply_position_policy(db: Session, bridge: BridgeClient, reason: str, policy: str | None = None,
                          user_id: int | None = None) -> list[dict]:
    """Apply the configured existing-position policy (PROTECT / REDUCE / CLOSE_ALL)."""
    account = tr.active_account(db)
    if account is None:
        return []
    tr.sync_positions(db, bridge, account)
    params = tr.risk_params(db, account.mode)
    pol = policy or params.existing_position_policy
    views = [risk.PositionView(p.ticket, p.symbol, p.direction, float(p.volume), float(p.open_price),
                               float(p.current_price or p.open_price), float(p.stop_loss) if p.stop_loss else None,
                               float(p.take_profit) if p.take_profit else None)
             for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id))]
    atr = {}
    for v in views:
        try:
            bars = bridge.rates(v.symbol, "H1", 60)
            if bars:
                import pandas as pd
                from app.analysis.indicators import atr as atr_fn
                df = pd.DataFrame(bars)
                atr[v.symbol] = float(atr_fn(df).iloc[-1])
        except Exception:  # noqa: BLE001
            pass
    actions = risk.emergency_actions(pol, views, params, atr)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    for a in actions:
        key = f"{account.id}:policy:{pol}:{a['ticket']}:{a['action']}:{stamp}"
        try:
            if a["action"] == "CLOSE":
                manage(db, bridge, action="CLOSE", position_ticket=a["ticket"], dedupe_key=key, user_id=user_id)
            elif a["action"] == "PARTIAL_CLOSE":
                spec = tr.spec_for(db.scalar(select(Symbol).where(Symbol.broker_symbol == a["symbol"])))
                vol = risk.floor_to_step(a["volume"], spec.volume_step)
                if vol >= spec.volume_min:
                    manage(db, bridge, action="PARTIAL_CLOSE", position_ticket=a["ticket"], dedupe_key=key, volume=vol,
                           user_id=user_id)
            elif a["action"] == "MODIFY":
                manage(db, bridge, action="MODIFY_SLTP", position_ticket=a["ticket"], dedupe_key=key,
                       stop_loss=a["stop_loss"], user_id=user_id)
            else:
                notify(db, "EXECUTION_FAILURE", "Manual attention required", a.get("detail", ""), a)
        except ExecutionBlocked as exc:
            notify(db, "EXECUTION_FAILURE", f"Policy action failed on {a['ticket']}", exc.reason, a)
    audit(db, user_id, "POSITION_POLICY", pol, {"reason": reason, "actions": actions})
    db.commit()
    return actions


def kill_switch(db: Session, bridge: BridgeClient | None, user_id: int | None, reason: str,
                policy: str | None = None) -> list[dict]:
    state = tr.get_state(db)
    state.kill_switch, state.kill_reason, state.auto_trading, state.updated_by = True, reason, False, user_id
    # block every order not yet sent
    for o in db.scalars(select(TradeOrder).where(TradeOrder.status == "PENDING")):
        o.status, o.status_detail = "REJECTED", "blocked by kill switch"
    audit(db, user_id, "KILL_SWITCH_ON", "", {"reason": reason})
    notify(db, "KILL_SWITCH", "EMERGENCY KILL SWITCH ACTIVATED", reason)
    system_event(db, "execution", "critical", f"Kill switch: {reason}")
    db.commit()
    publish("control", {"kill_switch": True, "reason": reason})
    return apply_position_policy(db, bridge, reason, policy, user_id) if bridge else []


def _news_status(db: Session, name: str, sym: Symbol | None) -> str:
    from app.news.service import status_for
    return status_for(db, sym, name)[0]


def _stale_limit() -> float:
    from app.core.config import get_settings
    return float(get_settings().max_quote_age_seconds)


def _log_adjustment(db: Session, account_id: int, ticket: int, field: str, prev: float | None, new: float | None,
                    reason: str, accepted: bool) -> None:
    from app.db.models_intel import PositionAdjustment
    db.add(PositionAdjustment(account_id=account_id, ticket=ticket, field=field, previous_value=prev, new_value=new,
                              reason=reason[:1000], accepted=accepted))
