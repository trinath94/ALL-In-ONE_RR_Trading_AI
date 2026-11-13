"""Background connection manager (GUI-independent; keeps running when the window is closed).

* MT5: attach / validate (``mt5link``), watchdog every 3 s — detects a closed or crashed terminal, re-attaches,
  detects an account change, keeps the daily equity baseline and reconciles unresolved journal entries.
* Execution: the **original connector code** (``mt5-connector/connector``: journal, idempotency, signature and
  replay checks, order building, retcode handling) serves every relayed request in-process. This package adds
  no execution logic of its own — only the local guard in front of ``execute``.
* Engine: ``EngineLink`` on its own asyncio thread (outbound WebSocket, heartbeat, reconnection).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from rr_connector import __version__, paths
from rr_connector import guard as g
from rr_connector.agent import EngineLink, Identity, register
from rr_connector.mt5link import Attached, ConnectError, MT5Link, probe
from rr_connector.store import Store
from rr_connector.terminals import Terminal, find_terminals
from rr_connector.vault import Vault

paths.ensure_connector_package()
from connector.app import ConnectorConfig, create_app  # noqa: E402  (original connector)
from connector.journal import Journal  # noqa: E402
from connector.service import CommandError  # noqa: E402

log = logging.getLogger("rr_connector.manager")
LIVE_PHRASE = "ENABLE LIVE TRADING"


class ConnectorManager:
    def __init__(self, store: Store, vault: Vault, adapter=None, finder: Callable[[], list[Terminal]] = find_terminals,
                 launcher=None, prober=probe, journal_path: str | None = None,
                 on_change: Callable[[dict], None] | None = None, watchdog_s: float = 3.0):
        if adapter is None:
            from connector.adapter import MT5Adapter
            adapter = MT5Adapter()
        self.store, self.vault, self.adapter, self.finder = store, vault, adapter, finder
        self.journal = Journal(journal_path or str(paths.journal_path()))
        self.link = MT5Link(adapter, launcher, prober)
        self.identity = Identity(store, vault)
        self.on_change = on_change or (lambda s: None)
        self.watchdog_s = watchdog_s
        self.lock = threading.RLock()
        self.terminals: list[Terminal] = []
        self.app = None
        self.http: httpx.AsyncClient | None = None
        self.engine = EngineLink(store, vault, self.identity, self.dispatch, self.status_payload, self._engine_state)
        self.snapshot: dict[str, Any] = {"mt5": "DISCONNECTED", "mt5_detail": "", "engine": "NOT_CONFIGURED",
                                         "engine_detail": "", "account": {}, "terminal": {}, "server_check": {},
                                         "mode": None, "warnings": [], "engine_rtt_ms": None, "last_heartbeat": None,
                                         "terminal_path": "", "local_kill_switch": bool(store.get("local_kill_switch", False))}
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- lifecycle ------------------------------------------------------------------------------------
    def start(self) -> None:
        self._stopping.clear()
        t1 = threading.Thread(target=self._engine_thread, name="engine-link", daemon=True)
        t2 = threading.Thread(target=self._watchdog, name="mt5-watchdog", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        if self.store.get("auto_connect") and self.store.get("mt5_address"):
            threading.Thread(target=self._auto_connect, name="auto-connect", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        self.engine.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.link.disconnect()

    def _engine_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.engine.run())
        if self.http:
            loop.run_until_complete(self.http.aclose())
            self.http = None                       # bound to this loop; rebuilt on the next start
        loop.close()

    def _auto_connect(self) -> None:
        delay = 5
        while not self._stopping.is_set() and self.snapshot["mt5"] != "CONNECTED":
            try:
                self.connect_mt5(self.store.get("mt5_address"), self.store.get("mt5_port"),
                                 self.store.get("terminal_path") or None)
                return
            except ConnectError as exc:
                if exc.code == "INPUT":
                    return
                self._stopping.wait(delay)
                delay = min(delay * 2, 60)

    # ---- state / events -------------------------------------------------------------------------------
    def _emit(self, **kv) -> None:
        with self.lock:
            self.snapshot.update(kv)
            snap = dict(self.snapshot)
        try:
            self.on_change(snap)
        except Exception:  # noqa: BLE001
            log.exception("on_change failed")

    def event(self, level: str, category: str, message: str) -> None:
        getattr(log, "warning" if level == "warning" else "error" if level == "error" else "info")(message)
        self.store.event(level, category, message)
        if level in ("warning", "error") or category == "audit":
            self.engine.send({"type": "event", "level": level if level != "audit" else "info", "message": message})

    def _engine_state(self, st: dict) -> None:
        self._emit(engine=st.get("engine"), engine_detail=st.get("detail", ""), engine_rtt_ms=st.get("rtt_ms"),
                   last_heartbeat=st.get("last_ack"))

    # ---- registration -------------------------------------------------------------------------------
    def register(self, engine_url: str, token: str, transport=None) -> dict:
        d = register(self.store, self.vault, self.identity, engine_url, token, transport)
        self._rebuild_app()
        return d

    # ---- MT5 -------------------------------------------------------------------------------------------
    def detect(self) -> list[Terminal]:
        self.terminals = self.finder()
        return self.terminals

    def connect_mt5(self, address: str, port: Any, terminal_exe: str | None = None) -> Attached:
        with self.lock:
            self._emit(mt5="CONNECTING", mt5_detail="Looking for MetaTrader 5…")
            try:
                self.link.disconnect()
                att = self.link.connect(address, port, self.detect(), terminal_exe)
            except ConnectError as exc:
                self._emit(mt5="ERROR" if exc.code != "CHOOSE_TERMINAL" else "CHOOSE", mt5_detail=str(exc))
                self.event("warning", "mt5", f"Connect failed: {exc}")
                raise
            self.store.update(mt5_address=address.strip(), mt5_port=int(port), terminal_path=att.terminal.exe,
                              auto_connect=True)
            self._rebuild_app()
            self._refresh(att.account, att.terminal_info)
            self._emit(mt5="CONNECTED", mt5_detail=att.server_check.get("detail", ""), server_check=att.server_check,
                       mode=att.mode, warnings=att.warnings, terminal_path=att.terminal.exe)
            self.event("info", "mt5", f"Connected to MT5 account {att.account.get('login')} on "
                                      f"{att.account.get('server')} ({att.mode}); {att.server_check.get('detail')}")
            return att

    def disconnect_mt5(self) -> None:
        with self.lock:
            self.link.disconnect()
            self.store.set("auto_connect", False)
            self.app = None
            self._emit(mt5="DISCONNECTED", mt5_detail="Disconnected by user", account={}, terminal={}, mode=None)
            self.event("info", "mt5", "MT5 disconnected by user")

    def _rebuild_app(self) -> None:
        """(Re)create the original connector app bound to the attached account and this agent's bridge secret."""
        secret = self.vault.get("bridge-secret")
        att = self.link.attached
        if not secret or not att:
            self.app = None
            return
        cfg = ConnectorConfig(shared_secret=secret, login=int(att.account["login"]), server=att.account["server"],
                              journal_path=str(self.journal.path) if hasattr(self.journal, "path") else "")
        app = create_app(cfg, self.adapter, self.journal, connect=False)
        svc = app.state.svc
        original = svc.execute

        def guarded(cmd: dict) -> dict:
            try:
                self.guard_check(cmd)
            except g.Blocked as exc:
                self.event("warning", "audit", f"Refused {cmd.get('action')} {cmd.get('symbol', '')} "
                                               f"{cmd.get('volume', '')}: {exc}")
                raise CommandError(403, f"blocked by RR MT5 Connector: {exc}") from None
            res = original(cmd)
            self.store.event("info", "trade", f"{cmd.get('action')} {cmd.get('symbol', '')} {cmd.get('volume', '')} -> "
                                              f"{res.get('state')} retcode={res.get('retcode')} ticket={res.get('order') or res.get('position_ticket') or ''}")
            return res
        svc.execute = guarded
        self.app = app

    def guard_check(self, cmd: dict) -> None:
        acct = self.adapter.account_info() or {}
        if not acct:
            raise g.Blocked("MT5 account information is unavailable")
        ds = self.store.get("day_start") or {}
        g.check(cmd, acct, len(self.adapter.positions()), ds.get("equity"),
                g.Limits.from_dict(self.store.get("guard")), self.store.get("live_authorization"),
                bool(self.store.get("local_kill_switch", False)))

    def _refresh(self, acct: dict, term: dict) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        ds = self.store.get("day_start") or {}
        if acct.get("login") and (ds.get("date") != today or ds.get("login") != acct.get("login")):
            self.store.set("day_start", {"date": today, "login": acct.get("login"), "equity": acct.get("equity")})
        self._emit(account=acct, terminal=term, mode=g.account_mode(acct) if acct else None,
                   live_authorized=g.live_authorized(self.store.get("live_authorization"), acct) if acct else False)

    def _watchdog(self) -> None:
        failures = 0
        while not self._stopping.wait(self.watchdog_s):
            att = self.link.attached
            if not att:
                continue
            try:
                term, acct = self.adapter.terminal_info(), self.adapter.account_info()
            except Exception as exc:  # noqa: BLE001 - terminal crashed / IPC broken
                term, acct = {}, {}
                log.warning("MT5 read failed: %s", exc)
            if not term or not acct:
                failures += 1
                if failures == 1:
                    self._emit(mt5="RECONNECTING", mt5_detail="MetaTrader 5 stopped responding — reconnecting…")
                    self.event("warning", "mt5", "MT5 terminal closed or not responding; reconnecting")
                try:
                    self.adapter.shutdown()
                    if self.adapter.attach(att.terminal.exe) and (self.adapter.account_info() or {}).get("login"):
                        failures = 0
                        self._emit(mt5="CONNECTED", mt5_detail="Reconnected to MetaTrader 5")
                        self.event("info", "mt5", "MT5 terminal reconnected")
                except Exception:  # noqa: BLE001
                    pass
                continue
            failures = 0
            if (int(acct.get("login") or 0), acct.get("server")) != (int(att.account.get("login") or 0), att.account.get("server")):
                self.event("warning", "audit", f"MT5 account changed from {att.account.get('login')}@"
                                               f"{att.account.get('server')} to {acct.get('login')}@{acct.get('server')}")
                att.account = acct
                att.mode = g.account_mode(acct)
                self._rebuild_app()
            att.account, att.mode = acct, g.account_mode(acct)
            if self.snapshot.get("mt5") != "CONNECTED":
                self._emit(mt5="CONNECTED", mt5_detail="")
            self._refresh(acct, term)
            if self.app is not None:
                for eid in self.journal.unresolved():
                    try:
                        self.app.state.svc.reconcile(eid)
                    except Exception:  # noqa: BLE001
                        log.exception("reconcile %s failed", eid)

    # ---- relay dispatch (engine -> original connector app) --------------------------------------------
    async def dispatch(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, bytes]:
        app = self.app
        if app is None:
            return 503, json.dumps({"detail": "MT5 is not connected on the PC (RR MT5 Connector)"}).encode()
        if self.http is None or getattr(self.http, "_rr_app", None) is not app:
            if self.http:
                await self.http.aclose()
            self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rr-mt5-agent",
                                          timeout=60)
            self.http._rr_app = app
        r = await self.http.request(method, path, headers=headers, content=body or None)
        return r.status_code, r.content

    # ---- safety controls -------------------------------------------------------------------------------
    def authorize_live(self, typed_login: str, phrase: str) -> None:
        acct = (self.adapter.account_info() or {}) if self.link.attached else {}
        if not acct:
            raise ValueError("Connect to MetaTrader 5 first.")
        if g.account_mode(acct) == "DEMO":
            raise ValueError("This is a DEMO account; live authorisation is not needed.")
        if str(typed_login).strip() != str(acct.get("login")):
            raise ValueError("The account number you typed does not match the connected account.")
        if phrase.strip() != LIVE_PHRASE:
            raise ValueError(f"Type {LIVE_PHRASE} exactly to confirm.")
        auth = {"login": int(acct["login"]), "server": acct.get("server"), "at": time.time()}
        self.store.set("live_authorization", auth)
        self._emit(live_authorized=True)
        self.event("warning", "audit", f"LIVE trading authorised on this PC for account {acct['login']}@{acct.get('server')}. "
                                       "RR AI Engine risk limits still apply.")

    def revoke_live(self) -> None:
        self.store.set("live_authorization", None)
        self._emit(live_authorized=False)
        self.event("warning", "audit", "LIVE trading authorisation revoked on this PC")

    def emergency_stop(self, reason: str = "Emergency stop pressed on the PC") -> None:
        self.store.set("local_kill_switch", True)
        self._emit(local_kill_switch=True)
        self.engine.send({"type": "kill_switch", "reason": reason})
        self.event("error", "audit", f"EMERGENCY STOP: {reason}. New positions are refused on this PC and the "
                                     "RR AI Engine kill switch was requested.")

    def release_emergency_stop(self) -> None:
        self.store.set("local_kill_switch", False)
        self._emit(local_kill_switch=False)
        self.event("warning", "audit", "Local emergency stop released (the RR AI Engine kill switch must be reset in the engine)")

    def set_limits(self, max_lot: float, max_open_positions: int, max_daily_loss_pct: float) -> None:
        if not (0 < max_lot <= 100 and 1 <= max_open_positions <= 500 and 0 < max_daily_loss_pct <= 100):
            raise ValueError("Limits out of range")
        self.store.set("guard", {"max_lot": max_lot, "max_open_positions": max_open_positions,
                                 "max_daily_loss_pct": max_daily_loss_pct})
        self.event("warning", "audit", f"PC safety limits set: max lot {max_lot}, max positions {max_open_positions}, "
                                       f"max daily loss {max_daily_loss_pct}%")

    # ---- heartbeat payload -----------------------------------------------------------------------------
    def status_payload(self) -> dict:
        s = dict(self.snapshot)
        acct, term = s.get("account") or {}, s.get("terminal") or {}
        return {
            "app_version": __version__, "state": s.get("mt5"),
            "terminal": {"connected": term.get("connected"), "trade_allowed": term.get("trade_allowed"),
                         "build": term.get("build"), "ping_ms": round(term["ping_last"] / 1000, 1) if term.get("ping_last") else None,
                         "path": s.get("terminal_path")},
            "account": {k: acct.get(k) for k in ("login", "server", "company", "currency", "balance", "equity", "margin",
                                                 "margin_free", "margin_level", "leverage", "trade_allowed")}
                       | {"account_type": g.account_mode(acct) if acct else None},
            "mt5_server": {"address": self.store.get("mt5_address"), "port": self.store.get("mt5_port"),
                           "validation": (s.get("server_check") or {}).get("result")},
            "live_authorized": bool(s.get("live_authorized")), "local_kill_switch": bool(s.get("local_kill_switch")),
            "guard": self.store.get("guard"),
        }
