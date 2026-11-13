"""Headless connector for RR Trading One (LOCAL WINDOWS MODE).

The desktop app supervises this process; there is no window. It reuses :class:`ConnectorManager` unchanged — and
through it the original connector code (journal, idempotency, HMAC + replay checks, retcode handling), the PC-side
guard (LIVE lock, lot/position/daily-loss caps, emergency stop) and the outbound relay to the engine.

    RR_MT5_Connector.exe --headless PROFILE.json      run, attached to the profile's terminal and account
    RR_MT5_Connector.exe --register-stdin ENGINE_URL  link to the engine; the one-time token is read from stdin
    RR_MT5_Connector.exe --detect-json                print the installed MetaTrader 5 terminals as JSON

Differences from the desktop connector, by design:

* **Explicit profile, never a guess.** The profile names the terminal executable, the broker server and the
  expected account login. If the terminal is logged in to another account, the connector disconnects and reports
  ``PROFILE_MISMATCH`` instead of trading on it.
* **Isolated instance.** Data lives in ``%LOCALAPPDATA%\\RR Trading One\\connector`` and credentials under their own
  Credential Manager service, so a standalone RR MT5 Connector linked to a remote engine is never overwritten.
* **Supervision files** in the data folder: ``status.json`` (atomic, every second; no secrets, login masked),
  ``stop.request`` (graceful shutdown), ``instance.lock`` (one instance only).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rr_connector import __version__

log = logging.getLogger("rr_connector.headless")
VAULT_SERVICE = "RR Trading One Connector"
STATUS_FILE = "status.json"
STOP_FILE = "stop.request"
LOCK_FILE = "instance.lock"
EXIT_OK, EXIT_USAGE, EXIT_NOT_REGISTERED, EXIT_ALREADY_RUNNING, EXIT_ERROR = 0, 2, 3, 4, 1


def home() -> Path:
    """Data folder of RR Trading One's connector instance (``RRMT5_HOME`` overrides, e.g. for tests)."""
    if os.environ.get("RRMT5_HOME"):
        base = Path(os.environ["RRMT5_HOME"])
    elif os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "RR Trading One" / "connector"
    else:
        base = Path.home() / ".rr-trading-one-connector"
    base.mkdir(parents=True, exist_ok=True)
    os.environ["RRMT5_HOME"] = str(base)   # paths.* (journal, config, logs) follow this instance's folder
    return base


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    terminal_exe: str
    server: str
    port: int
    expected_login: int
    engine_url: str

    @classmethod
    def load(cls, path: str) -> "Profile":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        p = cls(str(d["profile_id"])[:64], str(d.get("name") or d["profile_id"])[:128], str(d["terminal_exe"]),
                str(d["server"]).strip(), int(d.get("port") or 443), int(d["expected_login"]), str(d["engine_url"]))
        if not p.terminal_exe.lower().endswith("terminal64.exe"):
            raise ValueError("profile.terminal_exe must point to terminal64.exe")
        if not p.server or p.expected_login <= 0:
            raise ValueError("profile.server and profile.expected_login are required")
        if not (p.engine_url.startswith("http://127.0.0.1:") or p.engine_url.startswith("http://localhost:")
                or p.engine_url.startswith("https://")):
            raise ValueError("profile.engine_url must be the local loopback gateway or an https:// engine")
        return p


def mask_login(login) -> str:
    s = str(login or "")
    return ("•" * max(0, len(s) - 3) + s[-3:]) if s else ""


def write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)


class InstanceLock:
    """OS-level exclusive lock held for the process lifetime (released automatically if the process dies)."""

    def __init__(self, path: Path):
        self.f = open(path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise


def status_document(snapshot: dict, profile: Profile | None, state: str, detail: str, store) -> dict:
    """What the desktop's MT5 Manager shows (spec Part 6). No secrets; the login is masked."""
    acct, term = snapshot.get("account") or {}, snapshot.get("terminal") or {}
    from rr_connector import guard as g
    return {
        "version": __version__, "pid": os.getpid(), "updated_at": time.time(),
        "state": state, "detail": detail,
        "profile": {"id": profile.profile_id, "name": profile.name, "server": profile.server,
                    "expected_login": mask_login(profile.expected_login)} if profile else None,
        "terminal": {"path": snapshot.get("terminal_path") or (profile.terminal_exe if profile else ""),
                     "connected_to_broker": term.get("connected"), "algo_trading": term.get("trade_allowed"),
                     "build": term.get("build"),
                     "ping_ms": round(term["ping_last"] / 1000, 1) if term.get("ping_last") else None},
        "account": {"broker": acct.get("company"), "login": mask_login(acct.get("login")), "server": acct.get("server"),
                    "mode": g.account_mode(acct) if acct else None, "currency": acct.get("currency"),
                    "balance": acct.get("balance"), "equity": acct.get("equity"), "free_margin": acct.get("margin_free"),
                    "leverage": acct.get("leverage"), "trade_allowed": acct.get("trade_allowed")},
        "engine": {"state": snapshot.get("engine"), "detail": snapshot.get("engine_detail"),
                   "latency_ms": snapshot.get("engine_rtt_ms"), "last_heartbeat": snapshot.get("last_heartbeat"),
                   "agent_id": store.get("agent_id") if store else None},
        "mt5": {"state": snapshot.get("mt5"), "detail": snapshot.get("mt5_detail"),
                "server_check": (snapshot.get("server_check") or {}).get("result")},
        "warnings": list(snapshot.get("warnings") or []),
        "live_authorized": bool(snapshot.get("live_authorized")),
        "local_kill_switch": bool(snapshot.get("local_kill_switch")),
    }


def _open_store():
    from rr_connector import paths
    from rr_connector.store import Store
    from rr_connector.vault import Vault
    vault = Vault(service=VAULT_SERVICE)
    return Store(paths.config_db(), vault), vault


def detect_json() -> int:
    from rr_connector.terminals import find_terminals
    print(json.dumps([{"exe": t.exe, "folder": t.folder, "name": t.name, "version": t.version, "running": t.running}
                      for t in find_terminals()]))
    return EXIT_OK


def register_stdin(engine_url: str) -> int:
    """Link to the engine. The one-time token comes on stdin so it never appears in a process list."""
    from rr_connector.agent import Identity, RegistrationError, register
    token = sys.stdin.readline().strip()
    store, vault = _open_store()
    try:
        d = register(store, vault, Identity(store, vault), engine_url, token)
    except RegistrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_ERROR
    finally:
        store.close()
    print(json.dumps({"ok": True, "agent_id": d["agent_id"], "read_only_credential": bool(d.get("bridge_readonly_secret"))}))
    return EXIT_OK


def run(profile_path: str, adapter=None, finder=None, poll_s: float = 1.0) -> int:
    base = home()
    try:
        lock = InstanceLock(base / LOCK_FILE)  # noqa: F841 - held for the process lifetime
    except OSError:
        print("another RR Trading One connector instance is running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    status_path, stop_path = base / STATUS_FILE, base / STOP_FILE
    stop_path.unlink(missing_ok=True)
    try:
        profile = Profile.load(profile_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        write_atomic(status_path, status_document({}, None, "INVALID_PROFILE", str(exc), None))
        return EXIT_USAGE
    store, vault = _open_store()
    if not store.get("agent_id") or store.get("engine_url") != profile.engine_url.rstrip("/") \
            or not vault.get("bridge-secret"):
        write_atomic(status_path, status_document({}, profile, "NOT_REGISTERED",
                                                  "Link the connector to the engine first", store))
        store.close()
        return EXIT_NOT_REGISTERED
    from rr_connector.app import make_adapter
    from rr_connector.manager import ConnectorManager
    from rr_connector.mt5link import ConnectError
    from rr_connector.terminals import find_terminals
    adapter = adapter or make_adapter()
    kw = {"finder": finder} if finder else {"finder": find_terminals}
    # The profile decides which terminal and account to use: disable the manager's own auto-connect (it would race
    # this loop and could pick a terminal by itself).
    store.set("auto_connect", False)
    m = ConnectorManager(store, vault, adapter, **kw)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stopping.set())
        except ValueError:      # not in the main thread (tests)
            pass
    m.start()
    state, detail, next_try = "CONNECTING", "", 0.0
    try:
        while not stopping.is_set():
            if stop_path.exists():
                stop_path.unlink(missing_ok=True)
                break
            att = m.link.attached
            if att is None and time.monotonic() >= next_try:
                try:
                    m.connect_mt5(profile.server, profile.port, profile.terminal_exe)
                    att = m.link.attached
                except ConnectError as exc:
                    state, detail, next_try = exc.code, str(exc), time.monotonic() + 10
            snap = dict(m.snapshot)           # after the connect attempt: never report a stale state
            if att is not None:
                login = int((m.snapshot.get("account") or {}).get("login") or att.account.get("login") or 0)
                if login != profile.expected_login:
                    m.disconnect_mt5()
                    state = "PROFILE_MISMATCH"
                    detail = (f"MetaTrader 5 is logged in to account {mask_login(login)}, but this profile expects "
                              f"{mask_login(profile.expected_login)}. Disconnected; nothing is traded on it.")
                    next_try = time.monotonic() + 10
                else:
                    state, detail = snap.get("mt5") or "CONNECTED", snap.get("mt5_detail") or ""
            write_atomic(status_path, status_document(dict(m.snapshot), profile, state, detail, store))
            stopping.wait(poll_s)
    finally:
        m.stop()
        write_atomic(status_path, status_document({}, profile, "STOPPED", "Connector stopped", store))
        store.close()
    return EXIT_OK


def _attach_std_streams() -> None:
    """The packaged connector is a windowed (GUI-subsystem) executable. When the desktop app starts it with pipes,
    Windows passes the handles, but a windowed build can still start with ``sys.stdin`` / ``sys.stdout`` set to None.
    ``--register-stdin`` reads the one-time token from stdin and ``--detect-json`` answers on stdout, so re-attach
    any missing stream to the handle the parent passed. Without a handle the stream stays None, as before."""
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt
    get = ctypes.windll.kernel32.GetStdHandle
    get.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    for name, which, flags, mode in (("stdin", -10, os.O_RDONLY, "r"), ("stdout", -11, os.O_WRONLY, "w"),
                                     ("stderr", -12, os.O_WRONLY, "w")):
        if getattr(sys, name) is not None:
            continue
        handle = get(which)
        if not handle or handle == invalid:
            continue
        try:
            fd = msvcrt.open_osfhandle(handle, flags)
            setattr(sys, name, open(fd, mode, encoding="utf-8", buffering=1 if mode == "w" else -1))
        except OSError:
            pass


def main(argv: list[str]) -> int:
    from rr_connector import logsetup
    _attach_std_streams()
    home()
    logsetup.setup()
    if argv[0] == "--detect-json":
        return detect_json()
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return EXIT_USAGE
    if argv[0] == "--register-stdin":
        return register_stdin(argv[1])
    return run(argv[1])
