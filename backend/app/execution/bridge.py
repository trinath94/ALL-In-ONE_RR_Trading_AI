"""Authenticated client for the Windows MT5 connector.

Security: HTTPS with certificate verification (custom CA bundle supported) + per-request
HMAC-SHA256 signature over method, path, timestamp, nonce and body hash. The connector
rejects stale or replayed requests.

Reliability: idempotent reads are retried with backoff. Order submission is safe to retry
*only* because every order carries an execution_id that the connector journals before
touching MT5 — a retried POST returns the journalled result instead of re-executing.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.security import sign_request

log = logging.getLogger("bridge")


class BridgeError(Exception):
    pass


class BridgeUnavailable(BridgeError):
    """Network / TLS / timeout — outcome of a write is UNKNOWN and must be reconciled."""


class BridgeRejected(BridgeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


@dataclass
class BridgeConfig:
    base_url: str
    secret: str
    ca_bundle: str | None = None
    timeout: float = 10.0
    relay_agent_id: int | None = None   # set in relay mode (RR MT5 Connector desktop agent)

    @classmethod
    def from_settings(cls) -> "BridgeConfig":
        s = get_settings()
        if s.bridge_mode == "relay":
            from app.execution import relay
            agent = relay.resolve_agent()
            if agent is None:
                raise BridgeError("no RR MT5 Connector agent is registered (RR_BRIDGE_MODE=relay)")
            return cls("http://rr-mt5-agent", agent.secret, None, s.bridge_timeout_seconds, relay_agent_id=agent.id)
        return cls(s.bridge_url.rstrip("/"), s.bridge_shared_secret, s.bridge_ca_bundle, s.bridge_timeout_seconds)


class BridgeClient:
    def __init__(self, cfg: BridgeConfig | None = None, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg or BridgeConfig.from_settings()
        if not self.cfg.secret:
            raise BridgeError("RR_BRIDGE_SHARED_SECRET is not configured")
        verify: Any = self.cfg.ca_bundle or True
        if transport is None and self.cfg.relay_agent_id is not None:
            from app.execution.relay import RelayTransport
            transport = RelayTransport(self.cfg.relay_agent_id, self.cfg.timeout)
        self._http = httpx.Client(base_url=self.cfg.base_url, timeout=self.cfg.timeout, verify=verify,
                                  transport=transport)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, payload: dict | None = None, retries: int = 0) -> Any:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() if payload is not None else b""
        last: Exception | None = None
        for attempt in range(retries + 1):
            headers = {"Content-Type": "application/json", **sign_request(self.cfg.secret, method, path, body)}
            try:
                t0 = time.perf_counter()
                r = self._http.request(method, path, content=body or None, headers=headers)
                log.debug("bridge %s %s %s %.0fms", method, path, r.status_code, (time.perf_counter() - t0) * 1000)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise BridgeUnavailable(str(exc)) from exc
            if r.status_code >= 500 and attempt < retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except ValueError:
                    detail = r.text
                if r.status_code >= 500:
                    raise BridgeUnavailable(f"{r.status_code}: {detail}")
                raise BridgeRejected(r.status_code, detail)
            return r.json()
        raise BridgeUnavailable(str(last))

    # --- reads (retried) -----------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", retries=1)

    def account(self) -> dict:
        return self._request("GET", "/account", retries=2)

    def symbols(self) -> list[dict]:
        return self._request("GET", "/symbols", retries=2)

    def symbol(self, name: str) -> dict:
        return self._request("GET", f"/symbols/{name}", retries=2)

    def tick(self, name: str) -> dict:
        return self._request("GET", f"/ticks/{name}", retries=2)

    def rates(self, name: str, timeframe: str, count: int) -> list[dict]:
        return self._request("GET", f"/rates/{name}?timeframe={timeframe}&count={int(count)}", retries=2)

    def positions(self) -> list[dict]:
        return self._request("GET", "/positions", retries=2)

    def orders(self) -> list[dict]:
        return self._request("GET", "/orders", retries=2)

    def history(self, from_ts: int, to_ts: int) -> list[dict]:
        return self._request("GET", f"/history/deals?from_ts={int(from_ts)}&to_ts={int(to_ts)}", retries=2)

    def execution(self, execution_id: str) -> dict:
        """Journal lookup — authoritative answer on whether an execution_id reached MT5."""
        return self._request("GET", f"/executions/{execution_id}", retries=3)

    def margin(self, name: str, action: str, volume: float, price: float) -> dict:
        return self._request("POST", "/calc/margin", {"symbol": name, "action": action, "volume": volume,
                                                      "price": price}, retries=2)

    # --- writes (idempotent via execution_id) ----------------------------------------------
    def submit(self, command: dict) -> dict:
        if not command.get("execution_id"):
            raise BridgeError("execution_id is required for every trading command")
        return self._request("POST", "/executions", command, retries=2)
