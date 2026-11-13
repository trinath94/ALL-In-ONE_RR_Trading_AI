"""Account, symbol and position synchronisation with the MT5 connector.

The broker is the source of truth: application state is *replaced* by what MT5 reports,
never the other way round. Positions are never re-opened from stale application records.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (AccountSnapshot, ClosedPosition, OpenPosition, RiskConfig, Symbol, TradeOrder,
                           TradingAccount, TradingState)
from app.execution.bridge import BridgeClient
from app.notifications.service import notify
from app.risk.engine import AccountState, OpenRisk, RiskParams, SymbolSpec
from app.services.events import publish, system_event

log = logging.getLogger("trading")
UTC = timezone.utc


def get_state(db: Session) -> TradingState:
    st = db.get(TradingState, 1)
    if st is None:
        st = TradingState(id=1)
        db.add(st)
        db.flush()
    return st


def active_account(db: Session) -> TradingAccount | None:
    st = get_state(db)
    return db.scalar(select(TradingAccount).where(TradingAccount.mode == st.active_mode, TradingAccount.is_active.is_(True)))


def risk_params(db: Session, mode: str) -> RiskParams:
    rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == mode))
    return RiskParams.from_dict(rc.params if rc else None)


# ---- symbols --------------------------------------------------------------------------
CRYPTO = {"BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOGE", "BNB", "DOT"}
METALS = {"XAU", "XAG", "XPT", "XPD"}
FIAT = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK", "SGD", "HKD", "ZAR", "MXN",
        "TRY", "PLN", "CNH", "HUF", "CZK"}


def categorize(info: dict) -> str:
    base0 = (info.get("currency_base") or "").upper()
    if base0 in METALS:
        return "metals"
    if base0 in CRYPTO:
        return "crypto"
    path = (info.get("path") or "").lower()
    for key, cat in (("forex", "forex"), ("fx", "forex"), ("metal", "metals"), ("crypto", "crypto"),
                     ("indic", "indices"), ("index", "indices"), ("stock", "stocks"), ("share", "stocks"),
                     ("equit", "stocks"), ("energ", "commodities"), ("commod", "commodities"), ("oil", "commodities")):
        if key in path:
            return cat
    base, prof = (info.get("currency_base") or "").upper(), (info.get("currency_profit") or "").upper()
    if base in METALS:
        return "metals"
    if base in CRYPTO:
        return "crypto"
    if base in FIAT and prof in FIAT and base != prof:
        return "forex"
    return "other"


def canonical(name: str) -> str:
    """Strip broker prefixes/suffixes for display (e.g. 'EURUSD.r', 'm.EURUSD' -> 'EURUSD')."""
    core = name.split(".")
    best = max(core, key=len)
    return "".join(ch for ch in best if ch.isalnum()).upper()[:16]


def discover_symbols(db: Session, bridge: BridgeClient, broker_connection_id: int) -> dict:
    infos = bridge.symbols()
    seen = 0
    for info in infos:
        name = info["name"]
        sym = db.scalar(select(Symbol).where(Symbol.broker_connection_id == broker_connection_id,
                                             Symbol.broker_symbol == name))
        if sym is None:
            sym = Symbol(broker_connection_id=broker_connection_id, broker_symbol=name)
            db.add(sym)
        sym.canonical = canonical(name)
        sym.category = categorize(info)
        sym.description = info.get("description", "")[:255]
        sym.base_currency, sym.profit_currency = info.get("currency_base"), info.get("currency_profit")
        sym.margin_currency = info.get("currency_margin")
        sym.digits = int(info.get("digits", 5))
        sym.point = Decimal(str(info.get("point", 0)))
        sym.tick_size = Decimal(str(info.get("trade_tick_size", 0)))
        sym.tick_value = Decimal(str(info.get("trade_tick_value", 0)))
        sym.contract_size = Decimal(str(info.get("trade_contract_size", 0)))
        sym.volume_min = Decimal(str(info.get("volume_min", 0)))
        sym.volume_max = Decimal(str(info.get("volume_max", 0)))
        sym.volume_step = Decimal(str(info.get("volume_step", 0)))
        sym.stops_level = int(info.get("trade_stops_level", 0))
        sym.freeze_level = int(info.get("trade_freeze_level", 0))
        sym.trade_mode = int(info.get("trade_mode", 0))
        sym.filling_modes = int(info.get("filling_mode", 0))
        sym.tradable = sym.trade_mode == 4  # SYMBOL_TRADE_MODE_FULL
        sym.spec_raw = {k: v for k, v in info.items() if isinstance(v, (int, float, str, bool))}
        seen += 1
    system_event(db, "scanner", "info", f"Discovered {seen} broker symbols")
    return {"discovered": seen}


def spec_for(sym: Symbol, live: dict | None = None) -> SymbolSpec:
    """Prefer *live* spec from the connector (tick value changes with FX rates)."""
    src = live or sym.spec_raw or {}
    g = lambda k, d: float(src.get(k, d) or d)  # noqa: E731
    return SymbolSpec(
        symbol=sym.broker_symbol, digits=int(src.get("digits", sym.digits)), point=g("point", sym.point),
        tick_size=g("trade_tick_size", sym.tick_size), tick_value=g("trade_tick_value", sym.tick_value),
        tick_value_loss=float(src.get("trade_tick_value_loss") or 0) or None,
        contract_size=g("trade_contract_size", sym.contract_size), volume_min=g("volume_min", sym.volume_min),
        volume_max=g("volume_max", sym.volume_max), volume_step=g("volume_step", sym.volume_step),
        stops_level=int(src.get("trade_stops_level", sym.stops_level) or 0),
        profit_currency=src.get("currency_profit") or sym.profit_currency or "",
        base_currency=src.get("currency_base") or sym.base_currency or "",
        trade_allowed=int(src.get("trade_mode", sym.trade_mode) or 0) == 4,
    )


# ---- account ---------------------------------------------------------------------------
class IdentityError(Exception):
    pass


def sync_account(db: Session, bridge: BridgeClient, account: TradingAccount) -> AccountSnapshot:
    a = bridge.account()
    if int(a.get("login") or 0) != account.login or a.get("server") != account.server:
        raise IdentityError(f"Connected terminal is {a.get('login')}@{a.get('server')}, expected "
                            f"{account.login}@{account.server}")
    kind = a.get("account_type")
    if account.mode == "LIVE" and kind != "REAL":
        raise IdentityError(f"Account configured LIVE but broker reports {kind}")
    if account.mode == "DEMO" and kind not in ("DEMO", "CONTEST"):
        raise IdentityError(f"Account configured DEMO but broker reports {kind}")
    snap = AccountSnapshot(account_id=account.id, balance=Decimal(str(a["balance"])), equity=Decimal(str(a["equity"])),
                           margin=Decimal(str(a.get("margin") or 0)), free_margin=Decimal(str(a.get("margin_free") or 0)),
                           margin_level=a.get("margin_level"), leverage=int(a.get("leverage") or 0))
    account.currency = a.get("currency") or account.currency
    if not account.verified:
        account.verified, account.verified_at = True, datetime.now(UTC)
    db.add(snap)
    db.flush()
    publish("account", {"account_id": account.id, "mode": account.mode, "balance": float(snap.balance),
                        "equity": float(snap.equity), "free_margin": float(snap.free_margin),
                        "margin": float(snap.margin)})
    return snap


def _period_start(now: datetime, kind: str) -> datetime:
    d = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "week":
        return d - timedelta(days=d.weekday())
    if kind == "month":
        return d.replace(day=1)
    return d


def account_state(db: Session, account: TradingAccount, snap: AccountSnapshot) -> AccountState:
    now = datetime.now(UTC)

    def first_equity(since: datetime) -> float:
        v = db.scalar(select(AccountSnapshot.equity).where(AccountSnapshot.account_id == account.id,
                                                           AccountSnapshot.created_at >= since)
                      .order_by(AccountSnapshot.created_at.asc()).limit(1))
        return float(v) if v is not None else float(snap.equity)

    peak = db.scalar(select(func.max(AccountSnapshot.equity)).where(AccountSnapshot.account_id == account.id))
    closed = db.scalars(select(ClosedPosition.profit).where(ClosedPosition.account_id == account.id)
                        .order_by(ClosedPosition.closed_at.desc()).limit(50)).all()
    streak = 0
    for p in closed:
        if p < 0:
            streak += 1
        else:
            break
    return AccountState(account.login, account.server, account.mode, account.currency, float(snap.balance),
                        float(snap.equity), float(snap.margin), float(snap.free_margin), snap.leverage,
                        first_equity(_period_start(now, "day")), first_equity(_period_start(now, "week")),
                        first_equity(_period_start(now, "month")), float(peak or snap.equity), streak)


# ---- positions -------------------------------------------------------------------------
def sync_positions(db: Session, bridge: BridgeClient, account: TradingAccount) -> dict:
    live = bridge.positions()
    now = datetime.now(UTC)
    existing = {p.ticket: p for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id))}
    live_tickets = set()
    orders_by_tag = {}
    for o in db.scalars(select(TradeOrder).where(TradeOrder.account_id == account.id, TradeOrder.status == "CONFIRMED")
                        .order_by(TradeOrder.id.desc()).limit(500)):
        from app.execution.manager import mt5_tag
        orders_by_tag[mt5_tag(o.execution_id)] = o
    for p in live:
        t = int(p["ticket"])
        live_tickets.add(t)
        row = existing.get(t) or OpenPosition(account_id=account.id, ticket=t)
        row.symbol, row.direction = p["symbol"], "BUY" if int(p["type"]) == 0 else "SELL"
        row.volume, row.open_price = Decimal(str(p["volume"])), Decimal(str(p["price_open"]))
        row.current_price = Decimal(str(p.get("price_current") or p["price_open"]))
        row.stop_loss = Decimal(str(p["sl"])) if p.get("sl") else None
        row.take_profit = Decimal(str(p["tp"])) if p.get("tp") else None
        row.profit, row.swap = float(p.get("profit") or 0), float(p.get("swap") or 0)
        row.magic, row.comment = int(p.get("magic") or 0), (p.get("comment") or "")[:64]
        row.opened_at = datetime.fromtimestamp(int(p.get("time") or now.timestamp()), UTC)
        o = orders_by_tag.get(row.comment)
        if o:
            row.execution_id, row.strategy_codes = o.execution_id, o.strategy_codes
        row.synced_at = now
        if t not in existing:
            db.add(row)
    closed = [p for t, p in existing.items() if t not in live_tickets]
    if closed:
        _record_closed(db, bridge, account, closed)
        db.execute(delete(OpenPosition).where(OpenPosition.account_id == account.id,
                                              OpenPosition.ticket.in_([p.ticket for p in closed])))
    publish("positions", {"account_id": account.id, "count": len(live)})
    return {"open": len(live), "closed": len(closed)}


def _record_closed(db: Session, bridge: BridgeClient, account: TradingAccount, closed: list[OpenPosition]) -> None:
    since = int(min(p.opened_at for p in closed).timestamp()) - 60
    deals = bridge.history(since, int(datetime.now(UTC).timestamp()) + 60)
    for p in closed:
        exits = [d for d in deals if int(d.get("position_id") or 0) == p.ticket and int(d.get("entry", 0)) in (1, 3)]
        if not exits:
            log.warning("closed position %s has no exit deal yet; will retry", p.ticket)
            continue
        profit = sum(float(d.get("profit") or 0) for d in exits)
        comm = sum(float(d.get("commission") or 0) for d in deals if int(d.get("position_id") or 0) == p.ticket)
        swap = sum(float(d.get("swap") or 0) for d in exits)
        close_price = float(exits[-1]["price"])
        reason = {4: "STOP_LOSS", 5: "TAKE_PROFIT", 6: "STOP_OUT"}.get(int(exits[-1].get("reason", -1)), "CLOSED")
        r_mult = None
        if p.stop_loss:
            risk = abs(float(p.open_price) - float(p.stop_loss))
            if risk > 0:
                sgn = 1 if p.direction == "BUY" else -1
                r_mult = (close_price - float(p.open_price)) * sgn / risk
        if db.scalar(select(ClosedPosition.id).where(ClosedPosition.account_id == account.id,
                                                     ClosedPosition.ticket == p.ticket)):
            continue
        db.add(ClosedPosition(account_id=account.id, ticket=p.ticket, symbol=p.symbol, direction=p.direction,
                              volume=p.volume, open_price=p.open_price, close_price=Decimal(str(close_price)),
                              stop_loss=p.stop_loss, profit=profit, commission=comm, swap=swap, r_multiple=r_mult,
                              opened_at=p.opened_at, closed_at=datetime.fromtimestamp(int(exits[-1]["time"]), UTC),
                              close_reason=reason, strategy_codes=p.strategy_codes or [],
                              execution_id=p.execution_id))
        kind = {"STOP_LOSS": "STOP_LOSS_HIT", "TAKE_PROFIT": "TAKE_PROFIT_HIT"}.get(reason, "TRADE_CLOSED")
        notify(db, kind, f"{p.symbol} {p.direction} closed ({reason})", f"P&L {profit:+.2f} {account.currency}",
               {"ticket": p.ticket, "profit": profit})


def position_risk(db: Session, p: OpenPosition) -> OpenRisk:
    sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbol))
    risk = notional = 0.0
    if sym and float(sym.tick_size) > 0:
        per_unit = float(sym.tick_value) / float(sym.tick_size)
        notional = float(p.volume) * float(p.current_price or p.open_price) * per_unit
        if p.stop_loss:
            dist = (float(p.open_price) - float(p.stop_loss)) * (1 if p.direction == "BUY" else -1)
            risk = max(dist, 0) * per_unit * float(p.volume)  # 0 once stop is at/after break-even
    return OpenRisk(p.symbol, p.direction, float(p.volume), risk, notional, has_stop=p.stop_loss is not None)


def open_risks(db: Session, account: TradingAccount) -> list[OpenRisk]:
    return [position_risk(db, p) for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id))]


def notify_mt5(db: Session, ok: bool, detail: str = "") -> None:
    notify(db, "MT5_RECONNECTED" if ok else "MT5_DISCONNECTED", "MT5 connector " + ("reconnected" if ok else "DISCONNECTED"),
           detail)
