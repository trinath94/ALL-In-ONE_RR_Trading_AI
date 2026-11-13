"""Institutional Risk Engine.

Every order — automated, manual or Claude-recommended — must pass ``evaluate``. The engine
is deterministic and side-effect free; callers persist the returned ``RiskDecision``.

Position sizing uses the broker's live symbol specification:
    loss_per_lot = (stop_distance / tick_size) * tick_value_loss
MT5 reports ``trade_tick_value`` in the *account currency*, so no further conversion is
needed when it is present. If it is missing (some CFDs report 0), the engine converts
``contract_size * tick_size`` from the profit currency using an explicit conversion rate;
if no rate is supplied the order is rejected rather than guessed.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal


@dataclass
class RiskParams:
    initial_capital: float = 10_000
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 4.0
    max_monthly_loss_pct: float = 8.0
    max_drawdown_pct: float = 10.0
    max_open_positions: int = 5
    max_trades_per_symbol: int = 1
    max_total_exposure_pct: float = 3.0       # sum of open risk (% equity)
    max_lot_size: float = 5.0
    max_spread_points: float = 30
    max_slippage_points: float = 10
    max_correlated_exposure_pct: float = 1.5
    correlation_threshold: float = 0.7
    max_consecutive_losses: int = 4
    daily_profit_target_pct: float = 0.0       # 0 = disabled
    stop_after_profit_target: bool = False
    min_free_margin_pct: float = 50.0          # free margin after trade / equity
    max_leverage_exposure: float = 10.0        # total notional / equity
    max_size_multiplier: float = 1.0           # cap for martingale-style requests
    min_rr: float = 1.0
    require_take_profit: bool = True
    news_unknown_blocks: bool = True
    existing_position_policy: str = "PROTECT"  # PROTECT | REDUCE | CLOSE_ALL
    reduce_fraction: float = 0.5
    break_even_at_r: float = 1.0
    trailing_start_r: float = 1.5
    trailing_atr_mult: float = 2.0
    partial_close_at_r: float = 1.0
    partial_close_fraction: float = 0.5

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskParams":
        base = cls()
        for k, v in (d or {}).items():
            if hasattr(base, k):
                setattr(base, k, type(getattr(base, k))(v))
        return base

    def validate(self) -> list[str]:
        errs = []
        if not 0 < self.risk_per_trade_pct <= 5:
            errs.append("risk_per_trade_pct must be in (0, 5]")
        for k in ("max_daily_loss_pct", "max_weekly_loss_pct", "max_monthly_loss_pct", "max_drawdown_pct"):
            if getattr(self, k) <= 0:
                errs.append(f"{k} must be > 0")
        if self.max_daily_loss_pct > self.max_weekly_loss_pct or self.max_weekly_loss_pct > self.max_monthly_loss_pct:
            errs.append("loss limits must satisfy daily <= weekly <= monthly")
        if self.existing_position_policy not in ("PROTECT", "REDUCE", "CLOSE_ALL"):
            errs.append("existing_position_policy must be PROTECT, REDUCE or CLOSE_ALL")
        if self.max_size_multiplier < 1:
            errs.append("max_size_multiplier must be >= 1")
        return errs


@dataclass
class SymbolSpec:
    symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float            # account currency per 1 lot per tick (MT5 trade_tick_value)
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0         # points
    tick_value_loss: float | None = None
    profit_currency: str = ""
    base_currency: str = ""
    trade_allowed: bool = True


@dataclass
class AccountState:
    login: int
    server: str
    mode: str
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    day_start_equity: float
    week_start_equity: float
    month_start_equity: float
    peak_equity: float
    consecutive_losses: int = 0


@dataclass
class OpenRisk:
    symbol: str
    direction: str
    volume: float
    risk_amount: float           # money at risk to SL (0 if no SL -> flagged)
    notional: float
    has_stop: bool = True


@dataclass
class OrderRequest:
    symbol: str
    direction: str               # BUY | SELL
    entry: float
    stop_loss: float | None
    take_profit: float | None
    spread_points: float
    expected_slippage_points: float = 0
    size_multiplier: float = 1.0
    expires_at: datetime | None = None
    source: str = "AUTO"
    strategy_codes: list[str] = field(default_factory=list)
    requested_volume: float | None = None   # manual orders may request a volume (still capped)
    margin_required_per_lot: float | None = None  # from MT5 order_calc_margin when available
    conversion_rate: float | None = None    # profit ccy -> account ccy if tick_value unavailable


@dataclass
class ControlState:
    kill_switch: bool = False
    risk_lock: bool = False
    pause_new_entries: bool = False
    news_status: str = "UNKNOWN"     # CLEAR | RESTRICTED | UNKNOWN
    realized_today: float = 0.0
    anomaly_block: str | None = None  # Part 26: unresolved critical market/feed anomaly on this symbol


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class RiskDecision:
    approved: bool
    volume: float
    risk_amount: float
    risk_pct: float
    checks: list[Check]
    lock_trading: bool = False       # a hard account-level limit tripped
    lock_reason: str = ""

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]

    def dict(self) -> dict:
        d = asdict(self)
        d["reasons"] = self.reasons
        return d


def floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    q = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    return float(q * Decimal(str(step)))


def loss_per_lot(spec: SymbolSpec, stop_distance: float, conversion_rate: float | None = None) -> float | None:
    ticks = stop_distance / spec.tick_size
    tv = spec.tick_value_loss or spec.tick_value
    if tv and tv > 0:
        return ticks * tv
    if conversion_rate:
        return ticks * spec.contract_size * spec.tick_size * conversion_rate
    return None


def currencies(symbol: str, spec: SymbolSpec | None = None) -> tuple[str, str]:
    if spec and spec.base_currency and spec.profit_currency:
        return spec.base_currency, spec.profit_currency
    s = "".join(ch for ch in symbol.upper() if ch.isalpha())
    return (s[:3], s[3:6]) if len(s) >= 6 else (s, "")


def correlated_risk(req: OrderRequest, spec: SymbolSpec, positions: list[OpenRisk],
                    corr: dict[tuple[str, str], float], threshold: float) -> float:
    """Risk already on in positions that move with the new order (same-direction correlated,
    opposite-direction anti-correlated, or sharing the same currency leg exposure)."""
    total = 0.0
    nb, nq = currencies(req.symbol, spec)
    sgn_new = 1 if req.direction == "BUY" else -1
    for p in positions:
        rho = corr.get((req.symbol, p.symbol), corr.get((p.symbol, req.symbol)))
        sgn = 1 if p.direction == "BUY" else -1
        if p.symbol == req.symbol:
            rho = 1.0
        if rho is None:
            pb, pq = currencies(p.symbol)
            # currency-leg overlap: long EURUSD and long EURJPY both long EUR
            same = (nb and nb == pb and sgn == sgn_new) or (nq and nq == pq and sgn == sgn_new) or \
                   (nb and nb == pq and sgn != sgn_new) or (nq and nq == pb and sgn != sgn_new)
            if same:
                total += p.risk_amount
            continue
        if abs(rho) >= threshold and (sgn * sgn_new * math.copysign(1, rho)) > 0:
            total += p.risk_amount
    return total


def evaluate(req: OrderRequest, spec: SymbolSpec, acct: AccountState, positions: list[OpenRisk],
             params: RiskParams, control: ControlState, now: datetime | None = None,
             corr: dict[tuple[str, str], float] | None = None) -> RiskDecision:
    now = now or datetime.now(timezone.utc)
    checks: list[Check] = []
    lock, lock_reason = False, ""

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name, bool(ok), detail))
        return ok

    eq = acct.equity
    add("kill_switch", not control.kill_switch, "Global kill switch is active" if control.kill_switch else "off")
    add("risk_lock", not control.risk_lock, "Trading locked by a risk limit" if control.risk_lock else "off")
    if req.source != "SYSTEM":
        add("pause_new_entries", not control.pause_new_entries,
            "New entries are paused" if control.pause_new_entries else "accepting entries")
    add("symbol_tradable", spec.trade_allowed, "Symbol trading disabled by broker" if not spec.trade_allowed else "ok")
    add("account_equity", eq > 0, f"equity {eq:.2f}")
    if req.expires_at is not None:
        add("signal_expiry", now < req.expires_at, f"signal expired at {req.expires_at.isoformat()}"
            if now >= req.expires_at else "valid")

    # --- Stops & targets ---------------------------------------------------------
    sgn = 1 if req.direction == "BUY" else -1
    has_sl = req.stop_loss is not None and req.stop_loss > 0
    add("mandatory_stop_loss", has_sl, "Stop loss is mandatory" if not has_sl else f"SL {req.stop_loss}")
    sl_ok = has_sl and (req.entry - req.stop_loss) * sgn > 0
    if has_sl:
        add("stop_side", sl_ok, "Stop loss on wrong side of entry" if not sl_ok else "ok")
        dist_pts = abs(req.entry - req.stop_loss) / spec.point
        add("stops_level", dist_pts >= spec.stops_level,
            f"SL distance {dist_pts:.0f}pt < broker stops level {spec.stops_level}pt" if dist_pts < spec.stops_level else "ok")
    has_tp = req.take_profit is not None and req.take_profit > 0
    if params.require_take_profit:
        add("take_profit", has_tp, "Take profit required" if not has_tp else f"TP {req.take_profit}")
    if has_tp:
        tp_ok = (req.take_profit - req.entry) * sgn > 0
        add("tp_side", tp_ok, "Take profit on wrong side of entry" if not tp_ok else "ok")
        if sl_ok and tp_ok:
            rr = abs(req.take_profit - req.entry) / abs(req.entry - req.stop_loss)
            add("min_rr", rr >= params.min_rr, f"R:R {rr:.2f} < {params.min_rr}" if rr < params.min_rr else f"R:R {rr:.2f}")

    # --- Market conditions -----------------------------------------------------------
    add("spread", req.spread_points <= params.max_spread_points,
        f"spread {req.spread_points:.1f}pt > max {params.max_spread_points}pt" if req.spread_points > params.max_spread_points
        else f"spread {req.spread_points:.1f}pt")
    add("slippage", req.expected_slippage_points <= params.max_slippage_points,
        f"expected slippage {req.expected_slippage_points}pt > {params.max_slippage_points}pt"
        if req.expected_slippage_points > params.max_slippage_points else "ok")
    if control.news_status == "RESTRICTED":
        add("news", False, "High-impact news restriction window")
    elif control.news_status == "UNKNOWN":
        add("news", not params.news_unknown_blocks, "News feed unavailable: risk UNKNOWN (blocked by policy)"
            if params.news_unknown_blocks else "News risk UNKNOWN (allowed by policy)")
    else:
        add("news", True, "clear")
    if control.anomaly_block:
        add("market_anomaly", False, f"Critical anomaly active: {control.anomaly_block}")

    # --- Account-level loss limits (these lock trading) ------------------------------------
    def loss_pct(start: float) -> float:
        return max(0.0, (start - eq) / start * 100) if start > 0 else 0.0

    for name, start, limit in (("daily_loss", acct.day_start_equity, params.max_daily_loss_pct),
                               ("weekly_loss", acct.week_start_equity, params.max_weekly_loss_pct),
                               ("monthly_loss", acct.month_start_equity, params.max_monthly_loss_pct)):
        lp = loss_pct(start)
        if not add(name, lp < limit, f"{name.replace('_', ' ')} {lp:.2f}% >= limit {limit}%" if lp >= limit else f"{lp:.2f}%"):
            lock, lock_reason = True, f"{name} limit reached ({lp:.2f}%)"
    dd = loss_pct(acct.peak_equity)
    if not add("max_drawdown", dd < params.max_drawdown_pct,
               f"drawdown {dd:.2f}% >= {params.max_drawdown_pct}%" if dd >= params.max_drawdown_pct else f"{dd:.2f}%"):
        lock, lock_reason = True, f"max drawdown reached ({dd:.2f}%)"
    if not add("consecutive_losses", acct.consecutive_losses < params.max_consecutive_losses,
               f"{acct.consecutive_losses} consecutive losses" if acct.consecutive_losses >= params.max_consecutive_losses
               else str(acct.consecutive_losses)):
        lock, lock_reason = True, f"{acct.consecutive_losses} consecutive losses"
    if params.daily_profit_target_pct > 0 and params.stop_after_profit_target:
        gain = (eq - acct.day_start_equity) / acct.day_start_equity * 100 if acct.day_start_equity else 0
        add("daily_profit_target", gain < params.daily_profit_target_pct,
            f"daily target {params.daily_profit_target_pct}% reached ({gain:.2f}%)" if gain >= params.daily_profit_target_pct else f"{gain:.2f}%")

    # --- Portfolio limits -------------------------------------------------------------------
    add("max_open_positions", len(positions) < params.max_open_positions,
        f"{len(positions)} open >= max {params.max_open_positions}" if len(positions) >= params.max_open_positions else f"{len(positions)} open")
    same = [p for p in positions if p.symbol == req.symbol]
    add("max_trades_per_symbol", len(same) < params.max_trades_per_symbol,
        f"{len(same)} open on {req.symbol} >= {params.max_trades_per_symbol}" if len(same) >= params.max_trades_per_symbol else "ok")
    dup = [p for p in same if p.direction == req.direction]
    add("duplicate_position", not dup or params.max_trades_per_symbol > 1,
        f"{req.direction} {req.symbol} already open" if dup and params.max_trades_per_symbol <= 1 else "ok")
    unprotected = [p.symbol for p in positions if not p.has_stop]
    add("existing_positions_protected", not unprotected,
        f"open positions without stop loss: {unprotected}" if unprotected else "ok")

    # --- Position sizing ----------------------------------------------------------------------
    volume = risk_amount = 0.0
    if sl_ok and eq > 0:
        mult = min(max(req.size_multiplier, 0.0), params.max_size_multiplier)
        if req.size_multiplier > params.max_size_multiplier:
            add("size_multiplier", True, f"requested x{req.size_multiplier} capped to x{mult}")
        budget = eq * params.risk_per_trade_pct / 100 * mult
        # conservative stop distance: include spread and expected slippage
        stop_dist = abs(req.entry - req.stop_loss) + (req.spread_points + req.expected_slippage_points) * spec.point
        lpl = loss_per_lot(spec, stop_dist, req.conversion_rate)
        if not add("tick_value", lpl is not None and lpl > 0,
                   "Cannot value a tick in account currency (no tick value / conversion rate)" if not lpl else
                   f"loss/lot {lpl:.2f} {acct.currency}"):
            lpl = None
        if lpl:
            raw = budget / lpl
            if req.requested_volume is not None:
                raw = min(raw, req.requested_volume)
            volume = floor_to_step(min(raw, spec.volume_max, params.max_lot_size), spec.volume_step)
            if not add("volume_min", volume >= spec.volume_min,
                       f"risk budget {budget:.2f} supports {raw:.4f} lots < broker minimum {spec.volume_min}"
                       if volume < spec.volume_min else f"{volume} lots"):
                volume = 0.0
            if raw > params.max_lot_size:
                add("max_lot_size", True, f"capped at max lot size {params.max_lot_size}")
            risk_amount = volume * lpl
    risk_pct = risk_amount / eq * 100 if eq > 0 else 0

    # --- Exposure ------------------------------------------------------------------------------
    if volume > 0:
        open_risk = sum(p.risk_amount for p in positions)
        tot = (open_risk + risk_amount) / eq * 100
        add("total_exposure", tot <= params.max_total_exposure_pct,
            f"total open risk {tot:.2f}% > {params.max_total_exposure_pct}%" if tot > params.max_total_exposure_pct else f"{tot:.2f}%")
        cr = (correlated_risk(req, spec, positions, corr or {}, params.correlation_threshold) + risk_amount) / eq * 100
        add("correlated_exposure", cr <= params.max_correlated_exposure_pct,
            f"correlated risk {cr:.2f}% > {params.max_correlated_exposure_pct}%" if cr > params.max_correlated_exposure_pct else f"{cr:.2f}%")
        # account-currency notional: price units * (account ccy per price unit per lot)
        per_unit = (spec.tick_value / spec.tick_size) if spec.tick_value else spec.contract_size * (req.conversion_rate or 0)
        notional = volume * req.entry * per_unit
        lev = (sum(p.notional for p in positions) + notional) / eq
        add("leverage_exposure", lev <= params.max_leverage_exposure,
            f"notional/equity {lev:.1f}x > {params.max_leverage_exposure}x" if lev > params.max_leverage_exposure else f"{lev:.2f}x")
        if req.margin_required_per_lot is not None:
            need = req.margin_required_per_lot * volume
            after = (acct.free_margin - need) / eq * 100
            add("free_margin", after >= params.min_free_margin_pct,
                f"free margin after trade {after:.1f}% < {params.min_free_margin_pct}%" if after < params.min_free_margin_pct
                else f"{after:.1f}% free after trade")
        else:
            add("free_margin", False, "Margin requirement unavailable from broker; cannot validate margin")

    approved = all(c.passed for c in checks) and volume > 0
    return RiskDecision(approved, volume if approved else 0.0, risk_amount if approved else 0.0,
                        round(risk_pct, 4) if approved else 0.0, checks, lock, lock_reason)


@dataclass
class PositionView:
    ticket: int
    symbol: str
    direction: str
    volume: float
    open_price: float
    current_price: float
    stop_loss: float | None
    take_profit: float | None
    initial_stop: float | None = None
    partial_done: bool = False


def emergency_actions(policy: str, positions: list[PositionView], params: RiskParams,
                      atr: dict[str, float] | None = None) -> list[dict]:
    """Actions to apply to existing positions when a hard limit / kill switch trips.
    PROTECT: guarantee every position has a stop (ATR-based if missing), nothing closed.
    REDUCE:  PROTECT + partially close reduce_fraction of each position.
    CLOSE_ALL: close every position."""
    out = []
    for p in positions:
        if policy == "CLOSE_ALL":
            out.append({"action": "CLOSE", "ticket": p.ticket, "symbol": p.symbol, "volume": p.volume})
            continue
        if not p.stop_loss:
            a = (atr or {}).get(p.symbol)
            if a:
                sl = p.current_price - 2 * a if p.direction == "BUY" else p.current_price + 2 * a
                out.append({"action": "MODIFY", "ticket": p.ticket, "symbol": p.symbol, "stop_loss": sl})
            else:
                out.append({"action": "ALERT", "ticket": p.ticket, "symbol": p.symbol,
                            "detail": "Position has no stop and ATR unavailable — manual attention required"})
        if policy == "REDUCE":
            out.append({"action": "PARTIAL_CLOSE", "ticket": p.ticket, "symbol": p.symbol,
                        "volume": p.volume * params.reduce_fraction})
    return out


def manage_position(p: PositionView, params: RiskParams, atr: float | None, spec: SymbolSpec) -> list[dict]:
    """Break-even, partial profit and ATR trailing rules for an open position."""
    acts: list[dict] = []
    init = p.initial_stop or p.stop_loss
    if not init:
        return acts
    sgn = 1 if p.direction == "BUY" else -1
    r = abs(p.open_price - init)
    if r <= 0:
        return acts
    gained_r = (p.current_price - p.open_price) * sgn / r
    sl = p.stop_loss
    if params.partial_close_fraction > 0 and not p.partial_done and gained_r >= params.partial_close_at_r:
        vol = floor_to_step(p.volume * params.partial_close_fraction, spec.volume_step)
        if vol >= spec.volume_min and p.volume - vol >= spec.volume_min:
            acts.append({"action": "PARTIAL_CLOSE", "ticket": p.ticket, "volume": vol, "reason": f"{gained_r:.2f}R"})
    if gained_r >= params.break_even_at_r and (sl is None or (sl - p.open_price) * sgn < 0):
        sl = p.open_price + sgn * 2 * spec.point
        acts.append({"action": "MODIFY", "ticket": p.ticket, "stop_loss": round(sl, spec.digits), "reason": "break-even"})
    if atr and gained_r >= params.trailing_start_r:
        trail = p.current_price - sgn * params.trailing_atr_mult * atr
        if sl is None or (trail - sl) * sgn > spec.tick_size:
            acts = [a for a in acts if a.get("reason") != "break-even"]
            acts.append({"action": "MODIFY", "ticket": p.ticket, "stop_loss": round(trail, spec.digits), "reason": "trailing"})
    return acts
