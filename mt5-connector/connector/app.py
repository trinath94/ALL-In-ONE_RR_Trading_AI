"""HTTPS API exposed by the Windows connector to the Ubuntu application server only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from connector.adapter import TIMEFRAMES, MT5Adapter
from connector.journal import Journal
from connector.service import CommandError, ConnectorService, Identity

log = logging.getLogger("connector.api")


@dataclass
class ConnectorConfig:
    shared_secret: str
    login: int
    server: str
    password: str = ""
    terminal_path: str | None = None
    journal_path: str = "journal.sqlite3"
    allowed_ips: list[str] = field(default_factory=list)  # e.g. ["10.0.0.5/32"]
    max_skew: int = 30

    @classmethod
    def from_env(cls) -> "ConnectorConfig":
        secret = os.environ.get("RR_BRIDGE_SHARED_SECRET", "")
        password = os.environ.get("RR_MT5_PASSWORD", "")
        try:  # Prefer Windows Credential Manager (DPAPI-protected) when available.
            import keyring  # type: ignore
            secret = keyring.get_password("rr-mt5-connector", "bridge_secret") or secret
            password = keyring.get_password("rr-mt5-connector", "mt5_password") or password
        except Exception:  # noqa: BLE001
            pass
        return cls(shared_secret=secret, login=int(os.environ.get("RR_MT5_LOGIN", "0")),
                   server=os.environ.get("RR_MT5_SERVER", ""), password=password,
                   terminal_path=os.environ.get("RR_MT5_TERMINAL_PATH") or None,
                   journal_path=os.environ.get("RR_JOURNAL_PATH", "journal.sqlite3"),
                   allowed_ips=[s for s in os.environ.get("RR_ALLOWED_IPS", "").split(",") if s])


def _verify(cfg: ConnectorConfig, journal: Journal, method: str, path: str, body: bytes, headers) -> None:
    ts, nonce, sig = headers.get("x-rr-timestamp"), headers.get("x-rr-nonce"), headers.get("x-rr-signature")
    if not (ts and nonce and sig):
        raise HTTPException(401, "missing signature headers")
    try:
        if abs(int(time.time()) - int(ts)) > cfg.max_skew:
            raise HTTPException(401, "stale request")
    except ValueError:
        raise HTTPException(401, "bad timestamp") from None
    digest = hashlib.sha256(body).hexdigest()
    msg = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{digest}".encode()
    expected = base64.b64encode(hmac.new(cfg.shared_secret.encode(), msg, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "bad signature")
    if not journal.use_nonce(nonce):
        raise HTTPException(401, "replayed request")


def create_app(cfg: ConnectorConfig, adapter: MT5Adapter, journal: Journal | None = None,
               connect: bool = True) -> FastAPI:
    if len(cfg.shared_secret) < 32:
        raise RuntimeError("Bridge shared secret must be at least 32 characters")
    journal = journal or Journal(cfg.journal_path)
    svc = ConnectorService(adapter, journal, Identity(cfg.login, cfg.server))
    app = FastAPI(title="RR MT5 Connector", docs_url=None, redoc_url=None)
    app.state.svc = svc
    nets = [ipaddress.ip_network(n) for n in cfg.allowed_ips]

    if connect:
        if not adapter.initialize(cfg.terminal_path, cfg.login, cfg.password, cfg.server):
            log.error("MT5 initialize failed: %s", adapter.last_error())

        def watchdog():  # reconnect loop + journal reconciliation
            while True:
                time.sleep(5)
                try:
                    if not adapter.terminal_info().get("connected"):
                        log.warning("MT5 disconnected; reinitialising")
                        adapter.shutdown()
                        adapter.initialize(cfg.terminal_path, cfg.login, cfg.password, cfg.server)
                    for eid in journal.unresolved():
                        svc.reconcile(eid)
                except Exception:  # noqa: BLE001
                    log.exception("watchdog error")
        threading.Thread(target=watchdog, daemon=True).start()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if nets:
            ip = ipaddress.ip_address(request.client.host if request.client else "0.0.0.0")
            if not any(ip in n for n in nets):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
        body = await request.body()
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        try:
            _verify(cfg, journal, request.method, path, body, request.headers)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    @app.get("/health")
    def health():
        return svc.health()

    @app.get("/account")
    def account():
        return svc.account()

    @app.get("/symbols")
    def symbols():
        return adapter.symbols()

    @app.get("/symbols/{name}")
    def symbol(name: str):
        info = adapter.symbol_info(name)
        if not info:
            raise HTTPException(404, "symbol not found")
        return info

    @app.get("/ticks/{name}")
    def tick(name: str):
        t = adapter.tick(name)
        if not t:
            raise HTTPException(404, "no tick")
        return t

    @app.get("/rates/{name}")
    def rates(name: str, timeframe: str = "H1", count: int = 500):
        if timeframe not in TIMEFRAMES or not (1 <= count <= 50_000):
            raise HTTPException(400, "bad timeframe/count")
        return adapter.rates(name, timeframe, count)

    @app.get("/positions")
    def positions():
        return adapter.positions()

    @app.get("/orders")
    def orders():
        return adapter.orders()

    @app.get("/history/deals")
    def deals(from_ts: int, to_ts: int):
        return adapter.deals(from_ts, to_ts)

    @app.post("/calc/margin")
    async def margin(request: Request):
        b = json.loads(await request.body())
        act = adapter.const("ORDER_TYPE_BUY" if b["action"] == "BUY" else "ORDER_TYPE_SELL")
        return {"margin": adapter.calc_margin(act, b["symbol"], float(b["volume"]), float(b["price"]))}

    @app.post("/executions")
    async def execute(request: Request):
        cmd = json.loads(await request.body())
        try:
            return svc.execute(cmd)
        except CommandError as exc:
            raise HTTPException(exc.code, exc.detail) from None

    @app.get("/executions/{execution_id}")
    def execution(execution_id: str):
        row = journal.get(execution_id)
        if not row:
            return {"execution_id": execution_id, "state": "NOT_FOUND"}
        if row["state"] in ("SENDING", "UNKNOWN", "RECEIVED"):
            found = svc.reconcile(execution_id, row)
            if found:
                return {"execution_id": execution_id, **found}
        return {"execution_id": execution_id, "state": row["state"], **(row["result"] or {})}

    @app.get("/journal")
    def recent(limit: int = 100):
        return journal.recent(min(limit, 1000))

    return app
