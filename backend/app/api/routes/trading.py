"""Trading control center, accounts, orders, positions, signals, dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require
from app.core.security import encrypt_secret
from app.db.models import (AccountSnapshot, BrokerConnection, ClosedPosition, MT5Terminal, OpenPosition, RiskEvaluation,
                           Symbol, TradeExecution, TradeOrder, TradingAccount, TradingSignal, User)
from app.db.session import get_db
from app.execution import manager as em
from app.execution.bridge import BridgeClient, BridgeError
from app.services import trading as tr
from app.services.events import audit, publish

router = APIRouter(prefix="/api", tags=["trading"])
UTC = timezone.utc


def bridge_dep():
    try:
        b = BridgeClient()
    except BridgeError as exc:
        raise HTTPException(503, f"MT5 bridge not configured: {exc}") from None
    try:
        yield b
    finally:
        b.close()


def f(v):
    return float(v) if v is not None else None


# ---- control -------------------------------------------------------------------------------
def state_out(db: Session) -> dict:
    st = tr.get_state(db)
    acct = tr.active_account(db)
    return {"mode": st.active_mode, "auto_trading": st.auto_trading, "pause_new_entries": st.pause_new_entries,
            "kill_switch": st.kill_switch, "kill_reason": st.kill_reason, "risk_lock": st.risk_lock,
            "risk_lock_reason": st.risk_lock_reason, "startup_checks_passed": st.startup_checks_passed,
            "account": account_out(acct) if acct else None}


@router.get("/control/state")
def get_control(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return state_out(db)


class Toggle(BaseModel):
    enabled: bool
    confirm: bool = False


@router.post("/control/auto-trading")
def auto_trading(body: Toggle, request: Request, user: User = Depends(require("trade.control")),
                 db: Session = Depends(get_db)):
    st = tr.get_state(db)
    if body.enabled:
        if st.kill_switch or st.risk_lock:
            raise HTTPException(409, "Clear the kill switch / risk lock first (admin)")
        acct = tr.active_account(db)
        if acct is None or not acct.verified:
            raise HTTPException(409, "No verified active account for this mode")
        if st.active_mode == "LIVE":
            if "trade.live" not in __import__("app.api.deps", fromlist=["user_perms"]).user_perms(user):
                raise HTTPException(403, "LIVE auto trading requires trade.live permission")
            if not body.confirm:
                raise HTTPException(428, "Explicit confirmation required to enable LIVE auto trading")
    st.auto_trading, st.updated_by = body.enabled, user.id
    audit(db, user.id, "AUTO_TRADING_" + ("ON" if body.enabled else "OFF"), st.active_mode, ip=client_ip(request))
    publish("control", {"auto_trading": body.enabled})
    return state_out(db)


@router.post("/control/pause")
def pause(body: Toggle, request: Request, user: User = Depends(require("trade.control")), db: Session = Depends(get_db)):
    st = tr.get_state(db)
    st.pause_new_entries, st.updated_by = body.enabled, user.id
    audit(db, user.id, "PAUSE_ENTRIES_" + ("ON" if body.enabled else "OFF"), ip=client_ip(request))
    publish("control", {"pause_new_entries": body.enabled})
    return state_out(db)


class ModeIn(BaseModel):
    mode: str = Field(pattern="^(DEMO|LIVE)$")
    confirm_text: str = ""


@router.post("/control/mode")
def switch_mode(body: ModeIn, request: Request, user: User = Depends(require("trade.control")),
                db: Session = Depends(get_db)):
    """Switching mode never carries over settings, orders or state: auto trading is turned OFF,
    startup reconciliation must re-run for the new account, and each mode keeps its own risk config."""
    st = tr.get_state(db)
    if body.mode == st.active_mode:
        return state_out(db)
    if body.mode == "LIVE":
        from app.api.deps import user_perms
        if "trade.live" not in user_perms(user):
            raise HTTPException(403, "Only authorised users can activate LIVE trading")
        if body.confirm_text != "ENABLE LIVE TRADING":
            raise HTTPException(428, "Type 'ENABLE LIVE TRADING' to confirm")
        acct = db.scalar(select(TradingAccount).where(TradingAccount.mode == "LIVE", TradingAccount.is_active.is_(True)))
        if acct is None or not acct.live_approved_by:
            raise HTTPException(409, "No administrator-approved LIVE account")
    pending = db.scalar(select(func.count(TradeOrder.id)).where(TradeOrder.status.in_(["PENDING", "SENT", "UNKNOWN"])))
    if pending:
        raise HTTPException(409, f"{pending} executions unresolved; reconcile before switching")
    st.active_mode, st.auto_trading, st.startup_checks_passed, st.updated_by = body.mode, False, False, user.id
    audit(db, user.id, "MODE_SWITCH", body.mode, ip=client_ip(request))
    publish("control", {"mode": body.mode})
    return state_out(db)


class KillIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    policy: str | None = Field(default=None, pattern="^(PROTECT|REDUCE|CLOSE_ALL)$")


@router.post("/control/kill-switch")
def kill(body: KillIn, user: User = Depends(require("trade.control")), db: Session = Depends(get_db)):
    try:
        bridge = BridgeClient()
    except BridgeError:
        bridge = None
    actions = em.kill_switch(db, bridge, user.id, body.reason, body.policy)
    return {"state": state_out(db), "position_actions": actions,
            "warning": None if bridge else "Bridge unavailable: kill switch set, but no position actions applied"}


@router.post("/control/kill-switch/reset")
def kill_reset(request: Request, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    st = tr.get_state(db)
    st.kill_switch, st.kill_reason, st.risk_lock, st.risk_lock_reason = False, "", False, ""
    st.startup_checks_passed = False  # force full reconciliation before new entries
    audit(db, user.id, "KILL_SWITCH_RESET", ip=client_ip(request))
    publish("control", {"kill_switch": False})
    return state_out(db)


class ConfirmIn(BaseModel):
    confirm_text: str


@router.post("/control/close-all")
def close_all(body: ConfirmIn, user: User = Depends(require("trade.control")), db: Session = Depends(get_db),
              bridge: BridgeClient = Depends(bridge_dep)):
    if body.confirm_text != "CLOSE ALL":
        raise HTTPException(428, "Type 'CLOSE ALL' to confirm")
    acts = em.apply_position_policy(db, bridge, "manual close-all", "CLOSE_ALL", user.id)
    return {"actions": acts}


# ---- accounts ------------------------------------------------------------------------------
def account_out(a: TradingAccount) -> dict:
    return {"id": a.id, "login": a.login, "server": a.server, "mode": a.mode, "currency": a.currency,
            "verified": a.verified, "verified_at": a.verified_at, "live_approved": bool(a.live_approved_by),
            "is_active": a.is_active, "broker_connection_id": a.broker_connection_id}


class AccountIn(BaseModel):
    login: int
    server: str = Field(min_length=2, max_length=128)
    mode: str = Field(pattern="^(DEMO|LIVE)$")
    password: str | None = Field(default=None, max_length=256)
    broker_name: str = "Broker"
    terminal_name: str = "mt5-windows-1"
    bridge_url: str = ""
    timezone: str = "Etc/GMT-2"


@router.get("/accounts")
def accounts(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [account_out(a) for a in db.scalars(select(TradingAccount).order_by(TradingAccount.id))]


@router.post("/accounts")
def create_account(body: AccountIn, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    term = db.scalar(select(MT5Terminal).where(MT5Terminal.name == body.terminal_name))
    if term is None:
        term = MT5Terminal(name=body.terminal_name, bridge_url=body.bridge_url)
        db.add(term)
        db.flush()
    bc = BrokerConnection(broker_name=body.broker_name, server=body.server, terminal_id=term.id, timezone=body.timezone)
    db.add(bc)
    db.flush()
    a = TradingAccount(broker_connection_id=bc.id, login=body.login, server=body.server, mode=body.mode,
                       encrypted_password=encrypt_secret(body.password) if body.password else None)
    db.add(a)
    db.flush()
    audit(db, user.id, "ACCOUNT_CREATE", f"{body.login}@{body.server}", {"mode": body.mode})
    return account_out(a)


@router.post("/accounts/{account_id}/verify")
def verify_account(account_id: int, user: User = Depends(require("system.manage")), db: Session = Depends(get_db),
                   bridge: BridgeClient = Depends(bridge_dep)):
    a = db.get(TradingAccount, account_id)
    if not a:
        raise HTTPException(404)
    try:
        snap = tr.sync_account(db, bridge, a)
    except tr.IdentityError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.id, "ACCOUNT_VERIFIED", f"{a.login}@{a.server}")
    return {**account_out(a), "balance": f(snap.balance), "equity": f(snap.equity)}


@router.post("/accounts/{account_id}/approve-live")
def approve_live(account_id: int, body: ConfirmIn, user: User = Depends(require("trade.live")),
                 db: Session = Depends(get_db)):
    a = db.get(TradingAccount, account_id)
    if not a or a.mode != "LIVE":
        raise HTTPException(404, "LIVE account not found")
    if not a.verified:
        raise HTTPException(409, "Verify the account against the MT5 terminal first")
    if body.confirm_text != f"APPROVE {a.login}":
        raise HTTPException(428, f"Type 'APPROVE {a.login}' to confirm")
    a.live_approved_by, a.live_approved_at = user.id, datetime.now(UTC)
    audit(db, user.id, "LIVE_ACCOUNT_APPROVED", f"{a.login}@{a.server}")
    return account_out(a)


@router.post("/accounts/{account_id}/activate")
def activate(account_id: int, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    a = db.get(TradingAccount, account_id)
    if not a:
        raise HTTPException(404)
    for o in db.scalars(select(TradingAccount).where(TradingAccount.mode == a.mode)):
        o.is_active = o.id == a.id
    st = tr.get_state(db)
    if st.active_mode == a.mode:
        st.auto_trading, st.startup_checks_passed = False, False
    audit(db, user.id, "ACCOUNT_ACTIVATED", f"{a.login}@{a.server}", {"mode": a.mode})
    return account_out(a)


# ---- dashboard -----------------------------------------------------------------------------
@router.get("/dashboard/summary")
def summary(_: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = tr.active_account(db)
    out: dict = {"control": state_out(db), "account": None, "performance": None}
    if acct is None:
        return out
    snap = db.scalar(select(AccountSnapshot).where(AccountSnapshot.account_id == acct.id)
                     .order_by(AccountSnapshot.id.desc()).limit(1))
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week, month = day - timedelta(days=day.weekday()), day.replace(day=1)

    def pnl(since):
        return float(db.scalar(select(func.coalesce(func.sum(ClosedPosition.profit + ClosedPosition.commission +
                                                             ClosedPosition.swap), 0)).where(
            ClosedPosition.account_id == acct.id, ClosedPosition.closed_at >= since)) or 0)

    closed = db.scalars(select(ClosedPosition).where(ClosedPosition.account_id == acct.id)).all()
    wins = [c.profit for c in closed if c.profit > 0]
    losses = [-c.profit for c in closed if c.profit <= 0]
    opens = db.scalars(select(OpenPosition).where(OpenPosition.account_id == acct.id)).all()
    eqs = [float(e) for e in db.scalars(select(AccountSnapshot.equity).where(AccountSnapshot.account_id == acct.id)
                                        .order_by(AccountSnapshot.id))]
    max_dd = peak = 0.0
    for e in eqs:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100 if peak else 0)
    risks = tr.open_risks(db, acct)
    if snap:
        out["account"] = {"balance": f(snap.balance), "equity": f(snap.equity), "free_margin": f(snap.free_margin),
                          "margin": f(snap.margin), "margin_level": snap.margin_level, "leverage": snap.leverage,
                          "currency": acct.currency, "floating_pnl": round(sum(p.profit + p.swap for p in opens), 2),
                          "as_of": snap.created_at, "login": acct.login, "server": acct.server}
    out["performance"] = {
        "today_pnl": round(pnl(day), 2), "week_pnl": round(pnl(week), 2), "month_pnl": round(pnl(month), 2),
        "total_trades": len(closed), "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
        "active_positions": len(opens), "max_drawdown_pct": round(max_dd, 2),
        "open_risk_pct": round(sum(r.risk_amount for r in risks) / f(snap.equity) * 100, 3) if snap and f(snap.equity) else None,
    }
    return out


# ---- signals ---------------------------------------------------------------------------------
def signal_out(s: TradingSignal, sym: Symbol | None) -> dict:
    return {"id": s.id, "symbol": sym.broker_symbol if sym else None, "canonical": sym.canonical if sym else None,
            "category": sym.category if sym else None, "decision": s.decision, "grade": s.grade, "timeframe": s.timeframe,
            "entry": f(s.entry), "stop_loss": f(s.stop_loss), "tp1": f(s.tp1), "tp2": f(s.tp2), "tp3": f(s.tp3),
            "risk_reward": s.risk_reward, "score": s.score, "calibrated_confidence": s.calibrated_confidence,
            "confidence_sample": s.confidence_sample, "strategies": s.strategies, "regime": s.regime,
            "explanation": s.explanation, "ai_review": s.ai_review, "expires_at": s.expires_at, "status": s.status,
            "created_at": s.created_at}


@router.get("/signals")
def signals(_: User = Depends(current_user), db: Session = Depends(get_db), status: str | None = None,
            decision: str | None = None, limit: int = 200):
    q = select(TradingSignal, Symbol).join(Symbol, Symbol.id == TradingSignal.symbol_id)
    if status:
        q = q.where(TradingSignal.status == status)
    if decision:
        q = q.where(TradingSignal.decision == decision)
    return [signal_out(s, sym) for s, sym in db.execute(q.order_by(TradingSignal.id.desc()).limit(min(limit, 1000)))]


@router.get("/signals/{signal_id}")
def signal(signal_id: int, _: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.get(TradingSignal, signal_id)
    if not s:
        raise HTTPException(404)
    evs = db.scalars(select(RiskEvaluation).where(RiskEvaluation.signal_id == s.id)).all()
    return {**signal_out(s, db.get(Symbol, s.symbol_id)),
            "risk_evaluations": [{"approved": e.approved, "volume": f(e.volume), "risk_percent": e.risk_percent,
                                  "checks": e.checks, "reasons": e.reasons, "at": e.created_at} for e in evs]}


class ExecSignalIn(BaseModel):
    confirm_live: bool = False


@router.post("/signals/{signal_id}/execute")
def execute_signal(signal_id: int, body: ExecSignalIn, user: User = Depends(require("trade.manual")),
                   db: Session = Depends(get_db), bridge: BridgeClient = Depends(bridge_dep)):
    s = db.get(TradingSignal, signal_id)
    if not s or s.decision not in ("BUY", "SELL") or s.status not in ("NEW",):
        raise HTTPException(409, "Signal is not an executable BUY/SELL")
    sym = db.get(Symbol, s.symbol_id)
    acct = tr.active_account(db)
    try:
        o = em.open_position(db, bridge, symbol=sym.broker_symbol, direction=s.decision, stop_loss=f(s.stop_loss),
                             take_profit=f(s.tp2), source="MANUAL", user_id=user.id, signal=s,
                             dedupe_key=f"{acct.id if acct else 0}:signal:{s.id}:OPEN", expires_at=s.expires_at,
                             strategy_codes=sorted({x["strategy"] for x in s.strategies}), confirm_live=body.confirm_live)
    except em.ExecutionBlocked as exc:
        raise HTTPException(409, exc.reason) from None
    return order_out(o)


# ---- orders ------------------------------------------------------------------------------------
def order_out(o: TradeOrder) -> dict:
    return {"id": o.id, "execution_id": o.execution_id, "symbol": o.symbol, "action": o.action, "volume": f(o.volume),
            "price": f(o.price), "stop_loss": f(o.stop_loss), "take_profit": f(o.take_profit), "status": o.status,
            "status_detail": o.status_detail, "source": o.source, "created_at": o.created_at,
            "position_ticket": o.position_ticket, "signal_id": o.signal_id}


class ManualOrder(BaseModel):
    symbol: str
    direction: str = Field(pattern="^(BUY|SELL)$")
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT|STOP)$")
    price: float | None = None
    stop_loss: float
    take_profit: float | None = None
    volume: float | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=64)
    confirm_live: bool = False


@router.post("/orders")
def manual_order(body: ManualOrder, user: User = Depends(require("trade.manual")), db: Session = Depends(get_db),
                 bridge: BridgeClient = Depends(bridge_dep)):
    acct = tr.active_account(db)
    try:
        o = em.open_position(db, bridge, symbol=body.symbol, direction=body.direction, stop_loss=body.stop_loss,
                             take_profit=body.take_profit, source="MANUAL", user_id=user.id, order_type=body.order_type,
                             price=body.price, requested_volume=body.volume,
                             dedupe_key=f"{acct.id if acct else 0}:manual:{user.id}:{body.client_request_id}",
                             confirm_live=body.confirm_live)
    except em.ExecutionBlocked as exc:
        raise HTTPException(409, exc.reason) from None
    return order_out(o)


@router.get("/orders")
def orders(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 200):
    return [order_out(o) for o in db.scalars(select(TradeOrder).order_by(TradeOrder.id.desc()).limit(min(limit, 1000)))]


@router.get("/orders/{execution_id}")
def order(execution_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    o = db.scalar(select(TradeOrder).where(TradeOrder.execution_id == execution_id))
    if not o:
        raise HTTPException(404)
    ex = db.scalars(select(TradeExecution).where(TradeExecution.order_id == o.id)).all()
    return {**order_out(o), "executions": [{"retcode": e.retcode, "text": e.retcode_text, "price": f(e.filled_price),
                                            "volume": f(e.filled_volume), "slippage_points": e.slippage_points,
                                            "latency_ms": e.latency_ms, "at": e.executed_at} for e in ex]}


@router.post("/orders/reconcile")
def reconcile(_: User = Depends(require("trade.control")), db: Session = Depends(get_db),
              bridge: BridgeClient = Depends(bridge_dep)):
    return em.reconcile_unknown(db, bridge)


# ---- positions -----------------------------------------------------------------------------------
@router.get("/positions")
def positions(_: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = tr.active_account(db)
    if not acct:
        return []
    snap = db.scalar(select(AccountSnapshot).where(AccountSnapshot.account_id == acct.id).order_by(AccountSnapshot.id.desc()).limit(1))
    eq = f(snap.equity) if snap else None
    out = []
    for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == acct.id).order_by(OpenPosition.opened_at)):
        r = tr.position_risk(db, p)
        out.append({"ticket": p.ticket, "symbol": p.symbol, "direction": p.direction, "entry": f(p.open_price),
                    "current": f(p.current_price), "volume": f(p.volume), "stop_loss": f(p.stop_loss),
                    "take_profit": f(p.take_profit), "profit": p.profit, "swap": p.swap,
                    "risk_pct": round(r.risk_amount / eq * 100, 3) if r and eq else None,
                    "strategies": p.strategy_codes, "opened_at": p.opened_at,
                    "duration_s": int((datetime.now(UTC) - p.opened_at).total_seconds()),
                    "status": "PROTECTED" if p.stop_loss else "UNPROTECTED", "synced_at": p.synced_at})
    return out


@router.get("/positions/history")
def history(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 500, symbol: str | None = None):
    q = select(ClosedPosition).order_by(ClosedPosition.closed_at.desc())
    if symbol:
        q = q.where(ClosedPosition.symbol == symbol)
    return [{"ticket": c.ticket, "symbol": c.symbol, "direction": c.direction, "volume": f(c.volume),
             "entry": f(c.open_price), "exit": f(c.close_price), "profit": c.profit, "commission": c.commission,
             "swap": c.swap, "r_multiple": c.r_multiple, "opened_at": c.opened_at, "closed_at": c.closed_at,
             "reason": c.close_reason, "strategies": c.strategy_codes} for c in db.scalars(q.limit(min(limit, 5000)))]


class PositionAction(BaseModel):
    action: str = Field(pattern="^(MODIFY_SLTP|PARTIAL_CLOSE|CLOSE|BREAK_EVEN)$")
    stop_loss: float | None = None
    take_profit: float | None = None
    volume: float | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=64)


@router.post("/positions/{ticket}/action")
def position_action(ticket: int, body: PositionAction, user: User = Depends(require("trade.manual")),
                    db: Session = Depends(get_db), bridge: BridgeClient = Depends(bridge_dep)):
    acct = tr.active_account(db)
    action, sl = body.action, body.stop_loss
    if action == "BREAK_EVEN":
        p = db.scalar(select(OpenPosition).where(OpenPosition.ticket == ticket))
        if not p:
            raise HTTPException(404)
        action, sl = "MODIFY_SLTP", f(p.open_price)
    try:
        o = em.manage(db, bridge, action=action, position_ticket=ticket, user_id=user.id, stop_loss=sl,
                      take_profit=body.take_profit, volume=body.volume, source="MANUAL",
                      dedupe_key=f"{acct.id if acct else 0}:pos:{user.id}:{body.client_request_id}")
    except em.ExecutionBlocked as exc:
        raise HTTPException(409, exc.reason) from None
    return order_out(o)


@router.post("/positions/sync")
def sync(_: User = Depends(require("trade.manual")), db: Session = Depends(get_db),
         bridge: BridgeClient = Depends(bridge_dep)):
    acct = tr.active_account(db)
    if not acct:
        raise HTTPException(409, "No active account")
    tr.sync_account(db, bridge, acct)
    return tr.sync_positions(db, bridge, acct)
