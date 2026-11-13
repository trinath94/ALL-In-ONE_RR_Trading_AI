"""In-memory emulation of the MetaTrader5 Python API — TEST FIXTURE ONLY.

Used to exercise the connector's journal / idempotency / reconciliation logic on Linux CI.
It is never imported by production code.
"""
from __future__ import annotations

import time
from collections import namedtuple
from types import SimpleNamespace

import numpy as np

TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 1, 5, 6, 7, 8
ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = range(6)
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1, TIMEFRAME_MN1 = 16385, 16388, 16408, 32769, 49153

Account = namedtuple("Account", "login server currency balance equity margin margin_free margin_level leverage "
                                "trade_allowed trade_mode company name")
Terminal = namedtuple("Terminal", "connected trade_allowed build ping_last")
Symbol = namedtuple("Symbol", "name visible digits point trade_tick_size trade_tick_value trade_tick_value_loss "
                              "trade_contract_size volume_min volume_max volume_step trade_stops_level trade_freeze_level "
                              "trade_mode filling_mode currency_base currency_profit currency_margin description path "
                              "spread bid ask")
Tick = namedtuple("Tick", "time bid ask last volume")
Position = namedtuple("Position", "ticket symbol type volume price_open price_current sl tp profit swap magic comment time")
Deal = namedtuple("Deal", "ticket order position_id symbol type volume price profit commission swap magic comment time entry reason", defaults=(-1,))
Result = namedtuple("Result", "retcode deal order volume price bid ask comment request")


class FakeMT5:
    def __init__(self, login=1001, server="Demo-Server"):
        self.login, self.server = login, server
        self.connected = True
        self.positions: dict[int, dict] = {}
        self.pending: dict[int, dict] = {}
        self.deals: list[dict] = []
        self.next_ticket = 5000
        self.send_calls = 0
        self.fail_after_send = False      # simulate crash *after* broker accepted the order
        self.reject_retcode: int | None = None
        self.balance = 10_000.0
        self.symbols = {
            "EURUSD.r": dict(digits=5, point=1e-5, trade_tick_size=1e-5, trade_tick_value=1.0, trade_contract_size=100000,
                             currency_base="EUR", currency_profit="USD", bid=1.10000, ask=1.10010, trade_stops_level=10),
            "XAUUSD.r": dict(digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100,
                             currency_base="XAU", currency_profit="USD", bid=2400.00, ask=2400.30, trade_stops_level=50),
        }
        for k in list(globals()):
            if k.isupper():
                setattr(self, k, globals()[k])

    # lifecycle
    logged_in = True          # desktop-connector tests: the terminal's own session
    trade_mode = 0            # 0 DEMO, 1 CONTEST, 2 REAL

    def initialize(self, **kw):
        if "login" not in kw:     # attach to the running, already-authorised terminal
            return self.connected and self.logged_in
        return kw.get("login") == self.login

    def shutdown(self):
        pass

    def last_error(self):
        return (1, "Success")

    def terminal_info(self):
        return Terminal(self.connected, True, 4755, 12000)

    def account_info(self):
        if not self.logged_in:
            return None
        eq = self.balance + sum(p["profit"] for p in self.positions.values())
        return Account(self.login, self.server, "USD", self.balance, eq, 0.0, eq, 0.0, 100, True, self.trade_mode, "Fake", "Test")

    def _sym(self, name):
        s = self.symbols[name]
        return Symbol(name, True, s["digits"], s["point"], s["trade_tick_size"], s["trade_tick_value"],
                      s["trade_tick_value"], s["trade_contract_size"], 0.01, 100.0, 0.01, s["trade_stops_level"], 0, 4, 3,
                      s["currency_base"], s["currency_profit"], "USD", name, "Forex", 10, s["bid"], s["ask"])

    def symbols_get(self):
        return [self._sym(n) for n in self.symbols]

    def symbol_info(self, name):
        return self._sym(name) if name in self.symbols else None

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        s = self.symbols[name]
        return Tick(int(time.time()), s["bid"], s["ask"], 0.0, 0)

    def copy_rates_from_pos(self, name, tf, start, count):
        dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
        base = self.symbols[name]["bid"]
        t0 = int(time.time()) // 3600 * 3600 - count * 3600
        rows = [(t0 + i * 3600, base, base * 1.001, base * 0.999, base, 100, 10, 0) for i in range(count)]
        return np.array(rows, dtype=dt)

    def positions_get(self):
        return [Position(**p) for p in self.positions.values()]

    def orders_get(self):
        return [SimpleNamespace(_asdict=lambda o=o: o) for o in self.pending.values()]

    def history_deals_get(self, a, b):
        return [Deal(**d) for d in self.deals]

    def history_orders_get(self, a, b):
        return []

    def order_calc_margin(self, action, symbol, volume, price):
        return volume * self.symbols[symbol]["trade_contract_size"] * price / 100

    def order_check(self, req):
        return SimpleNamespace(_asdict=lambda: {"retcode": 0, "comment": "Done"})

    def order_send(self, req):
        self.send_calls += 1
        if self.reject_retcode:
            return Result(self.reject_retcode, 0, 0, 0.0, 0.0, 0.0, 0.0, "Rejected", None)
        self.next_ticket += 1
        t = self.next_ticket
        if req["action"] == TRADE_ACTION_DEAL and "position" not in req:
            self.positions[t] = dict(ticket=t, symbol=req["symbol"], type=0 if req["type"] == ORDER_TYPE_BUY else 1,
                                     volume=req["volume"], price_open=req["price"], price_current=req["price"],
                                     sl=req.get("sl", 0.0), tp=req.get("tp", 0.0), profit=0.0, swap=0.0,
                                     magic=req["magic"], comment=req["comment"], time=int(time.time()))
            self.deals.append(dict(ticket=t + 100000, order=t, position_id=t, symbol=req["symbol"], type=req["type"],
                                   volume=req["volume"], price=req["price"], profit=0.0, commission=0.0, swap=0.0,
                                   magic=req["magic"], comment=req["comment"], time=int(time.time()), entry=0))
        elif req["action"] == TRADE_ACTION_DEAL:
            p = self.positions[req["position"]]
            sgn = 1 if p["type"] == 0 else -1
            pnl = round((req["price"] - p["price_open"]) * sgn * req["volume"] * self.symbols[p["symbol"]]["trade_contract_size"], 2)
            self.deals.append(dict(ticket=t + 100000, order=t, position_id=p["ticket"], symbol=p["symbol"],
                                   type=req["type"], volume=req["volume"], price=req["price"], profit=pnl,
                                   commission=-0.7, swap=0.0, magic=req["magic"], comment=req["comment"],
                                   time=int(time.time()), entry=1))
            p["volume"] = round(p["volume"] - req["volume"], 2)
            if p["volume"] <= 0:
                del self.positions[req["position"]]
        elif req["action"] == TRADE_ACTION_SLTP:
            self.positions[req["position"]].update(sl=req["sl"], tp=req["tp"])
        if self.fail_after_send:
            raise ConnectionError("terminal connection lost after send")
        return Result(10009, t + 100000, t, req.get("volume", 0.0), req.get("price", 0.0), 0.0, 0.0, "Done", None)
