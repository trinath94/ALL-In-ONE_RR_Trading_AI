"""Local safety backstop on the Windows PC.

The RR AI Engine's Risk Engine remains the authority; this guard only adds PC-side refusals that the engine
cannot bypass. It never creates, modifies or enlarges an order.

* **Live accounts** (any account MT5 does not report as DEMO): every trading command is refused until the user
  explicitly authorises live trading for that exact login + server on this PC. Changing account revokes it.
* **Local emergency stop**: refuses every new exposure; closing / reducing / protecting stays allowed.
* **Hard caps** on new exposure: maximum lot per order, maximum open positions, maximum daily loss (% of the
  equity recorded at the start of the UTC day).
"""
from __future__ import annotations

from dataclasses import dataclass

OPENING = {"MARKET_BUY", "MARKET_SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}
MODE_NAMES = {0: "DEMO", 1: "CONTEST", 2: "LIVE"}


def account_mode(account: dict) -> str:
    return MODE_NAMES.get(account.get("trade_mode"), "LIVE")   # unknown => treated as LIVE (strictest)


class Blocked(Exception):
    pass


@dataclass
class Limits:
    max_lot: float = 1.0
    max_open_positions: int = 10
    max_daily_loss_pct: float = 5.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Limits":
        d = d or {}
        return cls(float(d.get("max_lot", 1.0)), int(d.get("max_open_positions", 10)),
                   float(d.get("max_daily_loss_pct", 5.0)))


def live_authorized(auth: dict | None, account: dict) -> bool:
    return bool(auth) and int(auth.get("login") or 0) == int(account.get("login") or -1) \
        and str(auth.get("server") or "") == str(account.get("server") or "")


def check(cmd: dict, account: dict, open_positions: int, day_start_equity: float | None, limits: Limits,
          live_auth: dict | None, local_kill: bool) -> None:
    """Raise :class:`Blocked` with a user-readable reason, or return to allow the command."""
    action = str(cmd.get("action", ""))
    mode = account_mode(account)
    if mode != "DEMO" and not live_authorized(live_auth, account):
        raise Blocked(f"{mode} account {account.get('login')}: live trading has not been authorised on this PC "
                      "(RR MT5 Connector > Enable live trading)")
    if action not in OPENING:
        return                                   # close / partial close / modify / cancel stay possible
    if local_kill:
        raise Blocked("emergency stop is active on the PC: new positions are refused")
    vol = float(cmd.get("volume") or 0)
    if vol <= 0 or vol > limits.max_lot + 1e-9:
        raise Blocked(f"volume {vol} exceeds the PC limit of {limits.max_lot} lots")
    if open_positions >= limits.max_open_positions:
        raise Blocked(f"{open_positions} positions open; PC limit is {limits.max_open_positions}")
    eq = float(account.get("equity") or 0)
    if day_start_equity and day_start_equity > 0:
        loss_pct = (day_start_equity - eq) / day_start_equity * 100
        if loss_pct >= limits.max_daily_loss_pct:
            raise Blocked(f"daily loss {loss_pct:.2f}% reached the PC limit of {limits.max_daily_loss_pct}%")
