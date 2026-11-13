"""Attach to the user's MetaTrader 5 terminal and validate the broker server they entered.

What the two fields mean (and do not mean): the MetaTrader5 Python integration talks to the **local terminal**,
which holds the broker session. A server address and port cannot log in to an account, and the connector never
asks for or guesses a trading password. CONNECT therefore:

1. finds the installed terminals and attaches to one that is running and **already logged in** (it starts the
   terminal if it is installed but not running, and asks the user to log in there if needed);
2. validates the entered server: it MATCHES when it equals the server name the terminal is logged in to
   (e.g. ``MetaQuotes-Demo``); otherwise the address:port must accept a TCP connection (reachability + latency),
   and the terminal's actual server is shown so the user can see which account is connected;
3. reads the account (balance, equity, margin, leverage, DEMO/LIVE) and trading permissions.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rr_connector.guard import account_mode
from rr_connector.terminals import Terminal

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-_ ]{0,253}[A-Za-z0-9])?$")


class ConnectError(Exception):
    def __init__(self, message: str, code: str = "ERROR", choices: list[Terminal] | None = None):
        super().__init__(message)
        self.code, self.choices = code, choices or []


@dataclass
class Attached:
    terminal: Terminal
    account: dict
    terminal_info: dict
    server_check: dict
    mode: str
    warnings: list[str] = field(default_factory=list)


def validate_inputs(address: str, port: Any) -> tuple[str, int]:
    address = (address or "").strip()
    if not address:
        raise ConnectError("Enter the MT5 server address (for example MetaQuotes-Demo or 185.10.20.30).", "INPUT")
    if len(address) > 255 or not (HOST_RE.match(address) or _is_ip(address)):
        raise ConnectError("The server address contains invalid characters.", "INPUT")
    try:
        p = int(str(port).strip())
    except ValueError:
        raise ConnectError("The server port must be a number between 1 and 65535.", "INPUT") from None
    if not 1 <= p <= 65535:
        raise ConnectError("The server port must be a number between 1 and 65535.", "INPUT")
    return address, p


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def probe(address: str, port: int, timeout: float = 5.0) -> dict:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return {"reachable": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except OSError as exc:
        return {"reachable": False, "error": str(exc)}


def server_check(address: str, port: int, account_server: str, prober: Callable = probe) -> dict:
    if address.strip().lower() == (account_server or "").strip().lower():
        return {"result": "MATCH", "detail": f"Terminal is logged in to {account_server}"}
    pr = prober(address, port)
    if pr.get("reachable"):
        return {"result": "REACHABLE", "latency_ms": pr["latency_ms"],
                "detail": f"{address}:{port} reachable in {pr['latency_ms']} ms; terminal is logged in to {account_server}"}
    return {"result": "UNREACHABLE", "detail": f"{address}:{port} is not reachable ({pr.get('error', 'no answer')})"}


class MT5Link:
    def __init__(self, adapter, launcher: Callable[[str], Any] | None = None, prober: Callable = probe,
                 sleep: Callable[[float], None] = time.sleep):
        self.adapter = adapter
        self.launcher = launcher or (lambda exe: subprocess.Popen([exe], close_fds=True))
        self.prober, self.sleep = prober, sleep
        self.attached: Attached | None = None

    def _try_attach(self, t: Terminal) -> dict | None:
        if not self.adapter.attach(t.exe):
            return None
        acct = self.adapter.account_info()
        if not acct or not acct.get("login"):
            self.adapter.shutdown()
            return None
        t.account_login, t.account_server = int(acct["login"]), acct.get("server")
        return acct

    def connect(self, address: str, port: Any, terminals: list[Terminal], chosen_exe: str | None = None,
                launch_wait_s: float = 60.0) -> Attached:
        address, port = validate_inputs(address, port)
        if not terminals:
            raise ConnectError("MetaTrader 5 is not installed on this PC. Install it from your broker or "
                               "https://www.metatrader5.com, log in to your account, then click CONNECT.", "NO_TERMINAL")
        pool = [t for t in terminals if t.exe == chosen_exe] if chosen_exe else list(terminals)
        if not pool:
            raise ConnectError("The selected MetaTrader 5 terminal was not found.", "NO_TERMINAL")
        if not any(t.running for t in pool):
            target = pool[0]
            self.launcher(target.exe)
            deadline = time.monotonic() + launch_wait_s
            while time.monotonic() < deadline:
                self.sleep(2)
                if self._try_attach(target):
                    target.running = True
                    self.adapter.shutdown()
                    break
            pool = [target]
        logged_in: list[tuple[Terminal, dict]] = []
        for t in pool:
            if not t.running:
                continue
            acct = self._try_attach(t)
            if acct:
                logged_in.append((t, acct))
                self.adapter.shutdown()
        if not logged_in:
            raise ConnectError("MetaTrader 5 is running but no trading account is logged in. Open MetaTrader 5, "
                               "log in to your account (File > Login to Trade Account), then click CONNECT. "
                               "RR MT5 Connector never asks for your trading password.", "NOT_LOGGED_IN")
        match = [x for x in logged_in if (x[1].get("server") or "").lower() == address.lower()]
        if match:
            chosen = match[0]
        elif len(logged_in) == 1:
            chosen = logged_in[0]
        else:
            raise ConnectError("Several MetaTrader 5 terminals are logged in. Choose the one to connect.",
                               "CHOOSE_TERMINAL", [t for t, _ in logged_in])
        t, _ = chosen
        if not self.adapter.attach(t.exe):
            raise ConnectError(f"Could not attach to MetaTrader 5 at {t.folder}.", "ATTACH_FAILED")
        acct, term = self.adapter.account_info(), self.adapter.terminal_info()
        check = server_check(address, port, acct.get("server", ""), self.prober)
        if check["result"] == "UNREACHABLE":
            self.adapter.shutdown()
            raise ConnectError(check["detail"] + ". Check the address and port, or enter the server name shown "
                               f"in MetaTrader 5 ({acct.get('server')}).", "UNREACHABLE")
        warnings = []
        if not term.get("connected"):
            warnings.append("The terminal is not connected to the broker right now.")
        if not term.get("trade_allowed"):
            warnings.append("Algo Trading is switched off in MetaTrader 5 (toolbar button 'Algo Trading').")
        if not acct.get("trade_allowed", True):
            warnings.append("The broker does not allow trading on this account.")
        self.attached = Attached(t, acct, term, check, account_mode(acct), warnings)
        return self.attached

    def disconnect(self) -> None:
        if self.attached:
            self.adapter.shutdown()
        self.attached = None
