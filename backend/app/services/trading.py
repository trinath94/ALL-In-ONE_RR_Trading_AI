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


AI_MODES = ("AI", "ENGINE_ONLY")


def engine_only_review() -> dict:
    """What the scanner records instead of an AI review while the operator runs the engine without AI."""
    return {"decision": "SKIPPED", "mode": "ENGINE_ONLY",
            "note": "AI review switched off by the operator (Run without AI); the Risk Engine decides at execution"}


def ai_unavailable_policy(db: Session) -> str:
    """Autonomous agent upgrade: "pause" (default) or "engine_only" when no AI answer is available in With AI mode."""
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1)
    return (c.ai_unavailable_policy if c is not None else None) or "pause"


def unavailable_review(db: Session, error: str) -> dict:
    """The review recorded when no AI answer exists (no provider, provider failure, budget reached). With the
    engine_only policy the engine's own validated signal continues to the Risk Engine; a rejection by the AI is never
    this case."""
    if ai_unavailable_policy(db) == "engine_only":
        return {"decision": "SKIPPED", "mode": "AI_UNAVAILABLE", "error": error[:500],
                "note": "AI unavailable; engine-only policy: the Risk Engine decides"}
    return {"decision": "UNREVIEWED", "error": error[:500]}


def engine_decided_review(reason: str) -> dict:
    """Engine-first operation: the engine's own validated signal goes to the Risk Engine without an AI call."""
    return {"decision": "SKIPPED", "mode": "ENGINE", "note": f"decided by the engine ({reason}); the Risk Engine decides"}


def review_policy(db: Session) -> dict:
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1)
    return {"candidate_review": (c.candidate_review if c else None) or "off",
            "daily_cap": int(c.review_daily_cap) if c and c.review_daily_cap is not None else 10,
            "confidence_below": float(c.review_confidence_below) if c and c.review_confidence_below is not None else 0.6}


def wants_ai_review(db: Session, sig) -> tuple[bool, str]:
    """Whether this candidate is sent to the AI for review (engine-first: by default never). Selective: only a
    candidate the engine itself is uncertain about (calibrated confidence missing or below the threshold), and at most
    the daily cap of reviews."""
    pol = review_policy(db)
    if pol["candidate_review"] == "all":
        return True, "every candidate is reviewed"
    if pol["candidate_review"] != "selective":
        return False, "candidate review off"
    conf = getattr(sig, "calibrated_confidence", None)
    if conf is not None and float(conf) >= pol["confidence_below"]:
        return False, f"engine confident ({float(conf):.2f} >= {pol['confidence_below']})"
    from app.db.models import AgentConversation
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.count(AgentConversation.id)).where(AgentConversation.purpose == "SIGNAL_REVIEW",
                                                                    AgentConversation.created_at >= day)) or 0
    if used >= pol["daily_cap"]:
        return False, f"daily review cap {pol['daily_cap']} reached"
    return True, "engine uncertain"


def auto_execution_block(st: TradingState, sig, policy: str = "pause") -> str | None:
    """Why a NEW signal would not be auto-executed now, or None if it is eligible (the Risk Engine still decides).
    One rule for the trading worker and the console's explanation.

    * AI mode (default): only candidates the AI review confirmed in the same direction.
    * Engine-only mode: the engine's own BUY/SELL, but only signals that were produced in engine-only mode, so a
      candidate the AI rejected or left unreviewed is never executed by switching the mode afterwards.
    """
    if sig.decision not in ("BUY", "SELL"):
        return "WAIT / NO_TRADE are never executed"
    if not st.auto_trading:
        return "auto trading is OFF"
    if st.auto_trading_since is not None and sig.created_at < st.auto_trading_since:
        return "produced before auto trading was switched on"
    review = sig.ai_review or {}
    d = review.get("decision")
    if (st.ai_mode or "AI") == "ENGINE_ONLY":
        if review.get("mode") == "PLAYBOOK":
            return review.get("note") or "the market playbook rules this trade out"
        if d == "SKIPPED" and review.get("mode") in ("ENGINE_ONLY", "ENGINE"):
            return None
        return "produced while the AI mode was on; engine-only mode executes only signals produced without AI"
    if review.get("mode") == "PLAYBOOK":
        return review.get("note") or "the market playbook rules this trade out"
    if d == "SKIPPED" and review.get("mode") == "ENGINE":
        return None                                  # engine-first: decided by the engine, never rejected by an AI
    if d == "SKIPPED" and review.get("mode") == "AI_UNAVAILABLE":
        return None if policy == "engine_only" else \
            "AI was unavailable and the policy is now pause: only AI-confirmed candidates are executed"
    if d == "SKIPPED":
        return "produced without AI review; the AI mode executes only AI-confirmed candidates"
    if d not in ("BUY", "SELL"):
        return "not confirmed by the AI review"
    if d != sig.decision:
        return f"AI review {d} does not confirm {sig.decision}"
    return None


def active_account(db: Session) -> TradingAccount | None:
    st = get_state(db)
    return db.scalar(select(TradingAccount).where(TradingAccount.mode == st.active_mode, TradingAccount.is_active.is_(True)))


def risk_params(db: Session, mode: str) -> RiskParams:
    rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == mode))
    return RiskParams.from_dict(rc.params if rc else None)


# ---- multi-account foundation ----------------------------------------------------------------
def account_risk_params(db: Session, account: TradingAccount | None, mode: str | None = None) -> RiskParams:
    """The limits that apply to one account: its mode's template (DEMO or LIVE) with the account's own overrides.
    Without overrides this is exactly the mode template, so single-account installations behave as before."""
    base = risk_params(db, account.mode if account is not None else (mode or "DEMO"))
    over = (account.risk_overrides or {}) if account is not None else {}
    if not over:
        return base
    return RiskParams.from_dict({**base.__dict__, **{k: v for k, v in over.items() if k in base.__dict__}})


def trading_accounts(db: Session, mode: str | None = None) -> list[TradingAccount]:
    """Accounts enabled for automatic trading (execution accounts), optionally of one mode."""
    q = select(TradingAccount).where(TradingAccount.trading_enabled.is_(True))
    if mode:
        q = q.where(TradingAccount.mode == mode)
    return list(db.scalars(q.order_by(TradingAccount.id)))


def is_reconciled(db: Session, account: TradingAccount) -> bool:
    """Startup reconciliation passed for this account. The active account keeps the original global flag."""
    return bool(get_state(db).startup_checks_passed) if account.is_active else bool(account.reconciled)


def set_reconciled(db: Session, account: TradingAccount, ok: bool) -> None:
    account.reconciled = ok
    if account.is_active:
        get_state(db).startup_checks_passed = ok


def execution_accounts(db: Session) -> list[TradingAccount]:
    """The accounts the engine trades now: the active account first, then every other account enabled for trading in
    the active mode. A LIVE account is included only while LIVE is the active mode (the existing LIVE switch) and only
    if it is verified and approved."""
    st = get_state(db)
    out: list[TradingAccount] = []
    act = active_account(db)
    if act is not None:
        out.append(act)
    for a in trading_accounts(db, st.active_mode):
        if act is not None and a.id == act.id:
            continue
        if not a.verified or (a.mode == "LIVE" and not a.live_approved_by):
            continue
        out.append(a)
    return out


def account_block(account: TradingAccount, symbol: str, strategy_codes) -> str | None:
    """Why this account would not auto-trade this setup, or None. Research-only accounts, symbols and strategies
    outside the account's lists are refused; the Risk Engine still decides everything else."""
    if not account.trading_enabled:
        return "the account is research-only (trading not enabled)"
    if account.allowed_symbols is not None and symbol not in account.allowed_symbols:
        return f"{symbol} is not among the account's symbols"
    if account.allowed_strategies is not None:
        codes = set(strategy_codes or [])
        if not codes & set(account.allowed_strategies):
            return "none of the signal's strategies is enabled for the account"
    return None


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
    from app.core.config import get_settings
    infos = bridge.symbols()
    seen = new = 0
    now = datetime.now(UTC)
    names: set[str] = set()
    for info in infos:
        name = info["name"]
        names.add(name)
        sym = db.scalar(select(Symbol).where(Symbol.broker_connection_id == broker_connection_id,
                                             Symbol.broker_symbol == name))
        if sym is None:
            # New symbols: scan flag from settings (RR Trading One: off until the user enables them). Existing symbols
            # keep the user's choice.
            sym = Symbol(broker_connection_id=broker_connection_id, broker_symbol=name,
                         scan_enabled=get_settings().new_symbols_scan_enabled)
            db.add(sym)
            new += 1
        sym.last_seen_at, sym.missing_since = now, None
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
    # Symbols the broker no longer offers: kept (history references them), marked missing and not tradable.
    missing = 0
    if names:
        for sym in db.scalars(select(Symbol).where(Symbol.broker_connection_id == broker_connection_id,
                                                   Symbol.broker_symbol.notin_(names))):
            if sym.missing_since is None:
                sym.missing_since = now
            sym.tradable = False
            missing += 1
    system_event(db, "scanner", "info", f"Discovered {seen} broker symbols ({new} new, {missing} no longer offered)")
    return {"discovered": seen, "new": new, "missing": missing}


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
def create_account_record(db: Session, *, login: int, server: str, mode: str, password: str | None = None,
                          broker_name: str = "Broker", terminal_name: str = "mt5-windows-1", bridge_url: str = "",
                          timezone_name: str = "Etc/GMT-2") -> TradingAccount:
    """Create a trading account with its terminal and broker connection (shared by the API and local setup)."""
    from app.core.security import encrypt_secret
    from app.db.models import BrokerConnection, MT5Terminal
    term = db.scalar(select(MT5Terminal).where(MT5Terminal.name == terminal_name))
    if term is None:
        term = MT5Terminal(name=terminal_name, bridge_url=bridge_url)
        db.add(term)
        db.flush()
    bc = BrokerConnection(broker_name=broker_name, server=server, terminal_id=term.id, timezone=timezone_name)
    db.add(bc)
    db.flush()
    a = TradingAccount(broker_connection_id=bc.id, login=login, server=server, mode=mode,
                       encrypted_password=encrypt_secret(password) if password else None)
    db.add(a)
    db.flush()
    return a


def activate_account(db: Session, a: TradingAccount) -> None:
    """Make `a` the active account of its mode. Switching the account of the active mode turns auto-trading off
    and requires the startup checks to pass again."""
    for o in db.scalars(select(TradingAccount).where(TradingAccount.mode == a.mode)):
        o.is_active = o.id == a.id
        o.reconciled = False          # every account of the mode is reconciled again under the new arrangement
    a.trading_enabled = True          # the account the engine trades is an execution account
    st = get_state(db)
    if st.active_mode == a.mode:
        st.auto_trading, st.startup_checks_passed = False, False


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
    return OpenRisk(p.symbol, p.direction, float(p.volume), risk, notional, has_stop=p.stop_loss is not None,
                    strategy_codes=tuple(p.strategy_codes or ()))


def open_risks(db: Session, account: TradingAccount) -> list[OpenRisk]:
    return [position_risk(db, p) for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id))]


def notify_mt5(db: Session, ok: bool, detail: str = "") -> None:
    notify(db, "MT5_RECONNECTED" if ok else "MT5_DISCONNECTED", "MT5 connector " + ("reconnected" if ok else "DISCONNECTED"),
           detail)
