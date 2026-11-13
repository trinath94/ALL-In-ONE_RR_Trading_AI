"""Thin wrapper around the official ``MetaTrader5`` Python package (Windows only).

All MT5 calls go through one lock: the MetaTrader5 module is not thread-safe.
"""
from __future__ import annotations

import threading
from typing import Any

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"]


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    return dict(obj)


class MT5Adapter:
    """Production adapter. ``mt5`` is the imported MetaTrader5 module (injected for testability)."""

    def __init__(self, mt5_module: Any | None = None):
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # type: ignore  # Windows-only dependency
        self.mt5 = mt5_module
        self.lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------------------
    def initialize(self, path: str | None, login: int, password: str, server: str, timeout_ms: int = 60_000) -> bool:
        with self.lock:
            kwargs = {"login": login, "password": password, "server": server, "timeout": timeout_ms}
            if path:
                kwargs["path"] = path
            return bool(self.mt5.initialize(**kwargs))

    def attach(self, path: str | None = None, timeout_ms: int = 60_000) -> bool:
        """Attach to a terminal that is already running and logged in (desktop connector).
        No credentials are passed: the terminal's own authorised session is used."""
        with self.lock:
            kwargs: dict = {"timeout": timeout_ms}
            if path:
                kwargs["path"] = path
            return bool(self.mt5.initialize(**kwargs))

    def shutdown(self) -> None:
        with self.lock:
            self.mt5.shutdown()

    def last_error(self) -> tuple:
        with self.lock:
            return tuple(self.mt5.last_error())

    # -- reads ----------------------------------------------------------------------
    def terminal_info(self) -> dict:
        with self.lock:
            return _as_dict(self.mt5.terminal_info())

    def account_info(self) -> dict:
        with self.lock:
            return _as_dict(self.mt5.account_info())

    def symbols(self) -> list[dict]:
        with self.lock:
            return [_as_dict(s) for s in (self.mt5.symbols_get() or [])]

    def symbol_info(self, name: str) -> dict:
        with self.lock:
            info = self.mt5.symbol_info(name)
            if info is not None and not info.visible:
                self.mt5.symbol_select(name, True)
                info = self.mt5.symbol_info(name)
            return _as_dict(info)

    def tick(self, name: str) -> dict:
        with self.lock:
            return _as_dict(self.mt5.symbol_info_tick(name))

    def rates(self, name: str, timeframe: str, count: int) -> list[dict]:
        tf = getattr(self.mt5, f"TIMEFRAME_{timeframe}")
        with self.lock:
            arr = self.mt5.copy_rates_from_pos(name, tf, 0, count)
        if arr is None:
            return []
        cols = arr.dtype.names
        return [{c: (r[c].item() if hasattr(r[c], "item") else r[c]) for c in cols} for r in arr]

    def positions(self) -> list[dict]:
        with self.lock:
            return [_as_dict(p) for p in (self.mt5.positions_get() or [])]

    def orders(self) -> list[dict]:
        with self.lock:
            return [_as_dict(o) for o in (self.mt5.orders_get() or [])]

    def deals(self, from_ts: int, to_ts: int) -> list[dict]:
        from datetime import datetime, timezone
        with self.lock:
            res = self.mt5.history_deals_get(datetime.fromtimestamp(from_ts, timezone.utc),
                                             datetime.fromtimestamp(to_ts, timezone.utc))
        return [_as_dict(d) for d in (res or [])]

    def history_orders(self, from_ts: int, to_ts: int) -> list[dict]:
        from datetime import datetime, timezone
        with self.lock:
            res = self.mt5.history_orders_get(datetime.fromtimestamp(from_ts, timezone.utc),
                                              datetime.fromtimestamp(to_ts, timezone.utc))
        return [_as_dict(o) for o in (res or [])]

    def calc_margin(self, action: int, symbol: str, volume: float, price: float) -> float | None:
        with self.lock:
            return self.mt5.order_calc_margin(action, symbol, volume, price)

    # -- writes ---------------------------------------------------------------------
    def order_check(self, request: dict) -> dict:
        with self.lock:
            return _as_dict(self.mt5.order_check(request))

    def order_send(self, request: dict) -> dict:
        with self.lock:
            res = self.mt5.order_send(request)
        d = _as_dict(res)
        if "request" in d and hasattr(d["request"], "_asdict"):
            d["request"] = d["request"]._asdict()
        return d

    def const(self, name: str) -> int:
        return int(getattr(self.mt5, name))
