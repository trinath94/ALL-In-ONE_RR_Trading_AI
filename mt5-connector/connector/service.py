"""Execution & synchronisation service for the MT5 terminal.

Every trading command is processed exactly once per ``execution_id``:
  1. reserve id in the journal (RECEIVED) — a duplicate id returns the journalled result;
  2. verify account identity (login + server) against the live terminal;
  3. build & pre-check the MT5 request (order_check);
  4. journal SENDING (fsync) -> order_send -> journal DONE/REJECTED with the MT5 result.
Only MT5 retcodes DONE / PLACED / DONE_PARTIAL count as success.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from connector.adapter import MT5Adapter
from connector.journal import Journal

log = logging.getLogger("connector")

SUCCESS = {10008, 10009, 10010}   # PLACED, DONE, DONE_PARTIAL
RETCODE_TEXT = {
    10004: "Requote", 10006: "Request rejected", 10007: "Request canceled", 10008: "Order placed",
    10009: "Request completed", 10010: "Partially filled", 10011: "Processing error", 10012: "Timeout",
    10013: "Invalid request", 10014: "Invalid volume", 10015: "Invalid price", 10016: "Invalid stops",
    10017: "Trade disabled", 10018: "Market closed", 10019: "Not enough money", 10020: "Prices changed",
    10021: "No quotes", 10024: "Too many requests", 10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client", 10030: "Unsupported filling mode", 10031: "No connection",
}
MAGIC = 26_062_026

ACTIONS = {"MARKET_BUY", "MARKET_SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "MODIFY_SLTP",
           "MODIFY_PENDING", "CANCEL_PENDING", "PARTIAL_CLOSE", "CLOSE"}


class CommandError(Exception):
    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def tag(execution_id: str) -> str:
    """Short deterministic tag stored in the MT5 order comment (max 31 chars) for reconciliation."""
    return "RR" + hashlib.sha1(execution_id.encode()).hexdigest()[:14]


@dataclass
class Identity:
    login: int
    server: str


class ConnectorService:
    def __init__(self, adapter: MT5Adapter, journal: Journal, identity: Identity):
        self.mt5 = adapter
        self.journal = journal
        self.identity = identity
        self.started = time.time()

    # -- health & identity ---------------------------------------------------------------
    def health(self) -> dict:
        t0 = time.perf_counter()
        term = self.mt5.terminal_info()
        acct = self.mt5.account_info()
        return {
            "connected": bool(term.get("connected")),
            "trade_allowed": bool(term.get("trade_allowed")) and bool(acct.get("trade_allowed", True)),
            "terminal_build": term.get("build"), "ping_last_us": term.get("ping_last"),
            "account_login": acct.get("login"), "server": acct.get("server"),
            "identity_ok": self.identity_ok(acct), "uptime_s": round(time.time() - self.started),
            "unresolved_executions": len(self.journal.unresolved()),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    def identity_ok(self, acct: dict | None = None) -> bool:
        acct = acct if acct is not None else self.mt5.account_info()
        return int(acct.get("login") or 0) == self.identity.login and acct.get("server") == self.identity.server

    def account(self) -> dict:
        a = self.mt5.account_info()
        mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(a.get("trade_mode"), "UNKNOWN")
        return {**{k: a.get(k) for k in ("login", "server", "currency", "balance", "equity", "margin", "margin_free",
                                         "margin_level", "leverage", "trade_allowed", "company", "name")},
                "account_type": mode, "identity_ok": self.identity_ok(a)}

    # -- trading ---------------------------------------------------------------------------
    def execute(self, cmd: dict) -> dict:
        eid = cmd.get("execution_id")
        if not eid or len(eid) > 64:
            raise CommandError(400, "execution_id required (<=64 chars)")
        if cmd.get("action") not in ACTIONS:
            raise CommandError(400, f"unknown action {cmd.get('action')}")
        created, _ = self.journal.reserve(eid, cmd)
        if not created:
            return self._replay(eid)
        try:
            acct = self.mt5.account_info()
            if not self.identity_ok(acct) or int(cmd.get("account_login", 0)) != self.identity.login \
                    or cmd.get("account_server") != self.identity.server:
                res = {"state": "REJECTED", "reason": "account identity mismatch", "retcode": None}
                self.journal.set_state(eid, "REJECTED", res)
                return {"execution_id": eid, **res}
            request = self._build(cmd)
        except CommandError as exc:
            res = {"state": "REJECTED", "reason": exc.detail, "retcode": None}
            self.journal.set_state(eid, "REJECTED", res)
            return {"execution_id": eid, **res}
        except Exception as exc:  # building failed before anything was sent
            res = {"state": "FAILED", "reason": f"pre-send error: {exc!r}", "retcode": None}
            self.journal.set_state(eid, "FAILED", res)
            return {"execution_id": eid, **res}

        check = self.mt5.order_check(request)
        if check and check.get("retcode") not in (0, None) and check.get("retcode") not in SUCCESS:
            res = {"state": "REJECTED", "reason": f"order_check: {check.get('comment')}", "retcode": check.get("retcode"),
                   "request": request}
            self.journal.set_state(eid, "REJECTED", res)
            return {"execution_id": eid, **res}

        self.journal.set_state(eid, "SENDING", {"request": request})
        t0 = time.perf_counter()
        try:
            r = self.mt5.order_send(request)
        except Exception as exc:
            self.journal.set_state(eid, "UNKNOWN", {"request": request, "reason": repr(exc)})
            return self._replay(eid)
        latency = (time.perf_counter() - t0) * 1000
        if not r:  # order_send returned None -> no answer; must reconcile, never assume
            self.journal.set_state(eid, "UNKNOWN", {"request": request, "reason": str(self.mt5.last_error())})
            return self._replay(eid)
        rc = r.get("retcode")
        res = {
            "state": "DONE" if rc in SUCCESS else "REJECTED", "retcode": rc,
            "retcode_text": RETCODE_TEXT.get(rc, r.get("comment", "")), "order": r.get("order"), "deal": r.get("deal"),
            "volume": r.get("volume"), "price": r.get("price"), "bid": r.get("bid"), "ask": r.get("ask"),
            "requested_price": request.get("price"), "latency_ms": round(latency, 2), "request": request,
            "slippage_points": self._slippage(request, r),
        }
        self.journal.set_state(eid, res["state"], res)
        return {"execution_id": eid, **res}

    def _slippage(self, req: dict, res: dict) -> float | None:
        try:
            info = self.mt5.symbol_info(req["symbol"])
            if req.get("price") and res.get("price"):
                return round(abs(float(res["price"]) - float(req["price"])) / float(info["point"]), 1)
        except Exception:  # noqa: BLE001
            return None
        return None

    def _replay(self, eid: str) -> dict:
        row = self.journal.get(eid)
        assert row is not None
        if row["state"] in ("SENDING", "UNKNOWN", "RECEIVED"):
            found = self.reconcile(eid, row)
            if found:
                return {"execution_id": eid, "replayed": True, **found}
            return {"execution_id": eid, "replayed": True, "state": "UNKNOWN",
                    "reason": "outcome not yet visible in MT5; do not resend — query again"}
        return {"execution_id": eid, "replayed": True, **(row["result"] or {}), "state": row["state"]}

    def reconcile(self, eid: str, row: dict | None = None) -> dict | None:
        """Search MT5 for evidence of an execution tagged with this id."""
        row = row or self.journal.get(eid)
        if not row:
            return None
        t = tag(eid)
        since = int(row["created_at"]) - 60
        now = int(time.time()) + 60
        for d in self.mt5.deals(since, now):
            if d.get("comment", "").startswith(t) or (d.get("magic") == MAGIC and d.get("comment") == t):
                res = {"state": "DONE", "retcode": 10009, "deal": d.get("ticket"), "order": d.get("order"),
                       "price": d.get("price"), "volume": d.get("volume"), "reconciled": True}
                self.journal.set_state(eid, "DONE", res)
                return res
        for o in self.mt5.orders() + self.mt5.history_orders(since, now):
            if o.get("comment", "").startswith(t):
                res = {"state": "DONE", "retcode": 10008, "order": o.get("ticket"), "price": o.get("price_open"),
                       "volume": o.get("volume_initial"), "reconciled": True}
                self.journal.set_state(eid, "DONE", res)
                return res
        # Not found. Only after a grace period is it safe to declare it never executed.
        if time.time() - row["updated_at"] > 120:
            res = {"state": "FAILED", "reason": "no trace in MT5 after 120s grace; never executed", "reconciled": True}
            self.journal.set_state(eid, "FAILED", res)
            return res
        return None

    def _filling(self, info: dict) -> int:
        fm = int(info.get("filling_mode") or 0)
        if fm & 1:
            return self.mt5.const("ORDER_FILLING_FOK")
        if fm & 2:
            return self.mt5.const("ORDER_FILLING_IOC")
        return self.mt5.const("ORDER_FILLING_RETURN")

    def _build(self, cmd: dict) -> dict:
        a = cmd["action"]
        sym = cmd.get("symbol")
        info = self.mt5.symbol_info(sym) if sym else {}
        if sym and not info:
            raise CommandError(404, f"symbol {sym} not found")
        c = self.mt5.const
        comment = tag(cmd["execution_id"])
        base = {"magic": MAGIC, "comment": comment, "deviation": int(cmd.get("deviation_points", 10))}
        if a in ("MARKET_BUY", "MARKET_SELL"):
            if info.get("trade_mode") == 0:
                raise CommandError(409, "trading disabled for symbol")
            t = self.mt5.tick(sym)
            price = t.get("ask") if a == "MARKET_BUY" else t.get("bid")
            if not price:
                raise CommandError(409, "no quote")
            req = {**base, "action": c("TRADE_ACTION_DEAL"), "symbol": sym, "volume": float(cmd["volume"]),
                   "type": c("ORDER_TYPE_BUY") if a == "MARKET_BUY" else c("ORDER_TYPE_SELL"), "price": price,
                   "type_filling": self._filling(info), "type_time": c("ORDER_TIME_GTC")}
            if cmd.get("stop_loss"):
                req["sl"] = float(cmd["stop_loss"])
            if cmd.get("take_profit"):
                req["tp"] = float(cmd["take_profit"])
            return req
        if a in ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"):
            req = {**base, "action": c("TRADE_ACTION_PENDING"), "symbol": sym, "volume": float(cmd["volume"]),
                   "type": c(f"ORDER_TYPE_{a}"), "price": float(cmd["price"]), "type_filling": c("ORDER_FILLING_RETURN"),
                   "type_time": c("ORDER_TIME_GTC")}
            if cmd.get("stop_loss"):
                req["sl"] = float(cmd["stop_loss"])
            if cmd.get("take_profit"):
                req["tp"] = float(cmd["take_profit"])
            return req
        if a == "MODIFY_SLTP":
            pos = self._position(cmd["position_ticket"])
            return {"action": c("TRADE_ACTION_SLTP"), "position": pos["ticket"], "symbol": pos["symbol"],
                    "sl": float(cmd.get("stop_loss") or pos.get("sl") or 0.0),
                    "tp": float(cmd.get("take_profit") or pos.get("tp") or 0.0), "magic": MAGIC}
        if a == "MODIFY_PENDING":
            return {"action": c("TRADE_ACTION_MODIFY"), "order": int(cmd["order_ticket"]), "price": float(cmd["price"]),
                    "sl": float(cmd.get("stop_loss") or 0), "tp": float(cmd.get("take_profit") or 0)}
        if a == "CANCEL_PENDING":
            return {"action": c("TRADE_ACTION_REMOVE"), "order": int(cmd["order_ticket"])}
        if a in ("CLOSE", "PARTIAL_CLOSE"):
            pos = self._position(cmd["position_ticket"])
            vol = float(pos["volume"]) if a == "CLOSE" else float(cmd["volume"])
            if vol <= 0 or vol > float(pos["volume"]) + 1e-9:
                raise CommandError(400, "invalid close volume")
            info = self.mt5.symbol_info(pos["symbol"])
            t = self.mt5.tick(pos["symbol"])
            is_buy = int(pos["type"]) == c("POSITION_TYPE_BUY")
            return {**base, "action": c("TRADE_ACTION_DEAL"), "position": pos["ticket"], "symbol": pos["symbol"],
                    "volume": vol, "type": c("ORDER_TYPE_SELL") if is_buy else c("ORDER_TYPE_BUY"),
                    "price": t["bid"] if is_buy else t["ask"], "type_filling": self._filling(info),
                    "type_time": c("ORDER_TIME_GTC")}
        raise CommandError(400, f"unsupported action {a}")

    def _position(self, ticket: Any) -> dict:
        for p in self.mt5.positions():
            if int(p["ticket"]) == int(ticket):
                return p
        raise CommandError(404, f"position {ticket} not found")
