"""Link to the RR AI Engine: device identity, signed registration, outbound WebSocket relay, heartbeat.

Only **outbound** HTTPS / WSS connections are made; nothing listens on the PC.
Protocol (engine side: backend/app/api/routes/mt5_agent.py; contract: docs/WINDOWS_CONNECTOR.md).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import platform
import random
import secrets
import ssl
import sys
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rr_connector import __version__
from rr_connector.store import Store
from rr_connector.vault import Vault

log = logging.getLogger("rr_connector.agent")
WS_PATH = "/api/v1/mt5/agent/ws"


class RegistrationError(Exception):
    pass


def _sha(b: bytes | str) -> str:
    return hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()


def normalise_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise RegistrationError("Enter the RR AI Engine address, e.g. https://engine.example.com")
    if "://" not in url:
        url = "https://" + url
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") \
            and not url.startswith("http://localhost"):
        raise RegistrationError("The RR AI Engine address must use HTTPS.")
    return url


def ws_url(engine_url: str) -> str:
    return ("wss://" + engine_url[8:] if engine_url.startswith("https://") else "ws://" + engine_url[7:]) + WS_PATH


def machine_guid() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            pass
    return f"{uuid.getnode():x}"


class Identity:
    """Device id + Ed25519 key pair. The private key lives only in the Credential Manager."""

    def __init__(self, store: Store, vault: Vault):
        self.store, self.vault = store, vault
        if not store.get("device_id"):
            store.set("device_id", _sha(f"{machine_guid()}:{secrets.token_hex(16)}")[:40])
        pem = vault.get("device-key")
        if not pem:
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            vault.set("device-key", pem)
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)

    @property
    def device_id(self) -> str:
        return self.store.get("device_id")

    @property
    def public_key(self) -> str:
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def sign(self, message: str) -> str:
        return base64.b64encode(self.key.sign(message.encode())).decode()


def tls_verify(store: Store) -> Any:
    ca = store.get("ca_file")
    return ca if ca else True


def register(store: Store, vault: Vault, identity: Identity, engine_url: str, token: str,
             transport: httpx.BaseTransport | None = None) -> dict:
    """One-time registration with a token created in the RR AI Engine (Settings > MT5 connectors)."""
    engine_url = normalise_url(engine_url)
    token = (token or "").strip()
    if len(token) < 16:
        raise RegistrationError("Enter the registration token shown in the RR AI Engine.")
    ts = time.time()
    msg = f"RRMT5-REGISTER\n{identity.device_id}\n{identity.public_key}\n{ts}\n{_sha(token)}"
    body = {"token": token, "device_id": identity.device_id, "device_name": platform.node()[:128],
            "device_info": {"os": platform.platform(), "machine": platform.machine(), "python": platform.python_version()},
            "public_key": identity.public_key, "app_version": __version__, "ts": ts, "signature": identity.sign(msg)}
    try:
        with httpx.Client(verify=tls_verify(store), timeout=20, transport=transport) as c:
            r = c.post(engine_url + "/api/v1/mt5/agent/register", json=body)
    except httpx.HTTPError as exc:
        raise RegistrationError(f"Cannot reach the RR AI Engine at {engine_url}: {exc}") from exc
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise RegistrationError(f"Registration refused ({r.status_code}): {detail}")
    d = r.json()
    vault.set("api-key", d["api_key"])
    vault.set("bridge-secret", d["bridge_secret"])
    store.update(engine_url=engine_url, agent_id=int(d["agent_id"]), agent_name=d.get("name", ""))
    store.event("info", "audit", f"Registered with RR AI Engine {engine_url} as agent {d['agent_id']}")
    return d


def signed_headers(identity: Identity, agent_id: int, api_key: str, method: str, path: str, body: bytes) -> dict:
    ts, nonce = str(time.time()), secrets.token_hex(12)
    sig = identity.sign(f"RRMT5-REQ\n{method}\n{path}\n{ts}\n{nonce}\n{_sha(body)}")
    return {"X-RR-Agent-Id": str(agent_id), "X-RR-Agent-Key": api_key, "X-RR-Agent-Ts": ts,
            "X-RR-Agent-Nonce": nonce, "X-RR-Agent-Sig": sig, "Content-Type": "application/json"}


Dispatcher = Callable[[str, str, dict, bytes], Awaitable[tuple[int, bytes]]]


class EngineLink:
    """Keeps one authenticated WebSocket open, reconnecting with backoff forever (until stopped)."""

    def __init__(self, store: Store, vault: Vault, identity: Identity, dispatch: Dispatcher,
                 status: Callable[[], dict], on_state: Callable[[dict], None], heartbeat_s: float = 5.0):
        self.store, self.vault, self.identity = store, vault, identity
        self.dispatch, self.status, self.on_state = dispatch, status, on_state
        self.heartbeat_s = heartbeat_s
        self.loop: asyncio.AbstractEventLoop | None = None
        self.outbox: asyncio.Queue | None = None
        self._stop: asyncio.Event | None = None
        self.state = {"engine": "NOT_CONFIGURED", "detail": "", "rtt_ms": None, "last_ack": None, "session": None}
        self._task: asyncio.Task | None = None

    def _set(self, **kv) -> None:
        self.state.update(kv)
        try:
            self.on_state(dict(self.state))
        except Exception:  # noqa: BLE001
            log.exception("state callback failed")

    # -- called from other threads ------------------------------------------------------------------
    def send(self, msg: dict) -> None:
        if self.loop and self.outbox is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.outbox.put_nowait, msg)
            except RuntimeError:
                pass                                # loop shutting down

    # -- main loop ----------------------------------------------------------------------------------
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.outbox = asyncio.Queue()
        self._stop = asyncio.Event()
        backoff = 1.0
        while not self._stop.is_set():
            agent_id, api_key = self.store.get("agent_id"), self.vault.get("api-key")
            url = self.store.get("engine_url")
            if not (agent_id and api_key and url):
                self._set(engine="NOT_CONFIGURED", detail="Run the one-time setup to link this PC to the RR AI Engine")
                await self._sleep(5)
                continue
            try:
                self._set(engine="CONNECTING", detail=url)
                await self._session(url, int(agent_id), api_key)
                backoff = 1.0
            except _Fatal as exc:
                self._set(engine="REJECTED", detail=str(exc))
                self.store.event("error", "engine", f"RR AI Engine refused this PC: {exc}")
                await self._sleep(60)
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError, ValueError) as exc:
                self._set(engine="DISCONNECTED", detail=f"{type(exc).__name__}: {exc}"[:300], session=None)
                self.store.event("warning", "engine", f"RR AI Engine connection lost: {exc}")
            if self._stop.is_set():
                break
            delay = min(backoff, 30.0) * (0.8 + random.random() * 0.4)
            backoff = min(backoff * 2, 30.0)
            await self._sleep(delay)
        self._set(engine="STOPPED", session=None)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        if self.loop and self._stop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass

    async def _session(self, url: str, agent_id: int, api_key: str) -> None:
        ssl_ctx = None
        if url.startswith("https://"):
            ca = self.store.get("ca_file")
            ssl_ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        async with websockets.connect(ws_url(url), ssl=ssl_ctx, ping_interval=10, ping_timeout=10,
                                      max_size=64 * 2 ** 20, open_timeout=15) as ws:
            ts, nonce = time.time(), secrets.token_hex(12)
            await ws.send(json.dumps({"type": "hello", "agent_id": agent_id, "api_key": api_key, "ts": ts, "nonce": nonce,
                                      "signature": self.identity.sign(f"RRMT5-HELLO\n{agent_id}\n{ts}\n{nonce}"),
                                      "app_version": __version__}))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if first.get("type") != "welcome":
                raise _Fatal(first.get("detail") or "authentication failed")
            self._set(engine="CONNECTED", detail=url, session=first.get("session_id"))
            self.store.event("info", "engine", f"Connected to RR AI Engine ({url})")
            tasks = [asyncio.create_task(self._reader(ws)), asyncio.create_task(self._heartbeat(ws)),
                     asyncio.create_task(self._writer(ws)), asyncio.create_task(self._stop.wait())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t.exception():
                    raise t.exception()

    async def _heartbeat(self, ws) -> None:
        while True:
            payload = {"type": "heartbeat", "ts": time.time(), **self.status(), "engine_rtt_ms": self.state.get("rtt_ms")}
            await ws.send(json.dumps(payload, default=str))
            await asyncio.sleep(self.heartbeat_s)

    async def _writer(self, ws) -> None:
        while True:
            msg = await self.outbox.get()
            await ws.send(json.dumps(msg, default=str))

    async def _reader(self, ws) -> None:
        async for raw in ws:
            m = json.loads(raw)
            kind = m.get("type")
            if kind == "request":
                asyncio.create_task(self._serve(ws, m))
            elif kind == "heartbeat_ack":
                rtt = round((time.time() - float(m.get("ts") or time.time())) * 1000, 1)
                self._set(rtt_ms=rtt, last_ack=time.time())
            elif kind == "error":
                raise _Fatal(m.get("detail") or "engine error")
        if ws.close_code in (4401, 4403):
            raise _Fatal("the engine revoked or rejected this PC's credentials")

    async def _serve(self, ws, m: dict) -> None:
        if float(m.get("deadline", 0)) < time.time():
            return                                   # expired in transit: never act on it
        try:
            status, body = await self.dispatch(m["method"], m["path"], m.get("headers") or {},
                                               base64.b64decode(m.get("body") or ""))
        except Exception as exc:  # noqa: BLE001
            log.exception("dispatch failed")
            status, body = 502, json.dumps({"detail": f"connector error: {exc!r}"}).encode()
        try:
            await ws.send(json.dumps({"type": "response", "id": m["id"], "status": status,
                                      "body": base64.b64encode(body).decode()}))
        except websockets.WebSocketException:
            pass                                     # engine will time out and reconcile via the journal


class _Fatal(Exception):
    """Credentials rejected — retry slowly; the user must re-run setup if the agent was revoked."""
