"""Supervised worker roles for RR Trading One (decision D1).

The original single worker (``app.workers.main``) runs heartbeat, sync, reconciliation, position management,
news, scanning and auto-execution in one loop, so stopping it would also stop scanning and position
protection. RR Trading One runs the *same functions* as three independently supervised roles:

* ``monitor``  - heartbeat, startup checks, account/position sync, journal reconciliation, signal expiry,
                 break-even/partial/trailing management, news refresh. Never opens positions.
* ``analysis`` - market scanning and signal generation (including the Claude review). Never executes: it
                 passes a no-op in place of the auto-execution callback.
* ``trading``  - auto-execution only. Stopped by default; started explicitly from the desktop. It considers
                 only signals created after it started, and only through the original ``auto_execute_factory``,
                 which still requires ``auto_trading``, a Claude-confirmed BUY/SELL, the Risk Engine, startup
                 checks and idempotent dedupe keys.

Usage: ``python -m app.workers.roles {monitor|analysis|trading}``

Nothing in the original worker is changed. The roles refuse to start while the monolithic worker holds its lock,
so the two modes cannot run side by side.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import TradingSignal
from app.db.session import session_scope
from app.execution import manager as em
from app.execution.bridge import BridgeClient, BridgeError, BridgeUnavailable
from app.news.service import refresh as refresh_news
from app.scanner.service import expire_signals, scan_once
from app.services import trading as tr
from app.services.events import agent_activity, get_redis
from app.workers import main as legacy

log = logging.getLogger("roles")
UTC = timezone.utc
ROLES = ("monitor", "analysis", "trading")
LOCK_TTL = 120
TRADING_POLL_SECONDS = 5
TRADING_BATCH = 20


def lock_key(role: str) -> str:
    return f"rr:role:{role}:lock"


class RoleConflict(RuntimeError):
    pass


def _text(v) -> str | None:
    """Redis values arrive as str (the app client decodes responses) or bytes (raw clients)."""
    return v.decode() if isinstance(v, bytes) else v


def owner_token() -> str:
    """Identifies this process's container. A container restarted by Docker keeps its hostname, so it can reclaim
    a lock its previous incarnation could not release (e.g. after a hard kill)."""
    return f"{socket.gethostname()}"


def acquire(role: str, r=None, owner: str | None = None) -> None:
    """Take the role lock. Refuse while the monolithic worker or another instance of this role runs."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    r = r or get_redis()
    owner = owner or owner_token()
    if r.exists(legacy.LOCK_KEY):
        raise RoleConflict("the monolithic worker (app.workers.main) is running; roles cannot run alongside it")
    if r.set(lock_key(role), owner, nx=True, ex=LOCK_TTL):
        return
    if _text(r.get(lock_key(role))) == owner:
        r.expire(lock_key(role), LOCK_TTL)
        return
    raise RoleConflict(f"another '{role}' process holds its lock")


def release(role: str, r=None, owner: str | None = None) -> None:
    """Release only a lock this owner holds."""
    r = r or get_redis()
    if _text(r.get(lock_key(role))) == (owner or owner_token()):
        r.delete(lock_key(role))


def connect_bridge() -> BridgeClient | None:
    """Build the bridge client, or None while no MT5 connector is registered (normal on a fresh install)."""
    try:
        return BridgeClient()
    except BridgeError as exc:
        log.info("waiting for the MT5 connector: %s", exc)
        return None


def publish_disconnected(reason: str, r=None) -> None:
    """Make the true state visible to analysis (skips scanning) and to the System Monitor."""
    (r or get_redis()).set("rr:bridge:health", json.dumps(
        {"connected": False, "error": reason, "ts": datetime.now(UTC).isoformat()}), ex=60)


def current_agent_id() -> int | None:
    """Registered relay agent, if relay mode is active (None in direct mode)."""
    key = current_agent_key()
    return key[0] if key else None


def current_agent_key() -> tuple[int, str] | None:
    """(agent id, credential fingerprint). Re-linking the same PC keeps the id but rotates its secrets; the
    fingerprint (SHA-256 of the stored ciphertexts) changes with them. Never decrypts anything."""
    if get_settings().bridge_mode != "relay":
        return None
    import hashlib
    from app.execution import relay
    with session_scope() as db:
        agent = relay.select_agent(db)
        if agent is None:
            return None
        material = f"{agent.bridge_secret_enc}|{agent.bridge_readonly_secret_enc or ''}"
        return agent.id, hashlib.sha256(material.encode()).hexdigest()


def _noop_execute(_signal_id: int) -> None:
    """Analysis never executes; candidates are left for the trading role."""


# ---- monitor ----------------------------------------------------------------------------------------------------
def monitor_cycle(bridge: BridgeClient, last_ok: list[bool], clock: dict) -> bool:
    """One monitor iteration. Mirrors the non-scanning part of the original worker loop."""
    ok = legacy.heartbeat(bridge, last_ok)
    if not ok:
        return False
    with session_scope() as db:
        passed = tr.get_state(db).startup_checks_passed
    if not passed:
        good, report = legacy.startup_checks(bridge)
        log.info("startup checks: %s %s", good, report)
    try:
        with session_scope() as db:
            acct = tr.active_account(db)
            if acct:
                tr.sync_account(db, bridge, acct)
                tr.sync_positions(db, bridge, acct)
            em.reconcile_unknown(db, bridge)
            expire_signals(db)
        legacy.manage_positions(bridge)
        refresh_symbol_catalogue(bridge)
        if time.time() - clock.get("news", 0.0) > 3600:
            with session_scope() as db:
                refresh_news(db)
            clock["news"] = time.time()
    except tr.IdentityError as exc:
        with session_scope() as db:
            em.kill_switch(db, bridge, None, f"Account identity changed: {exc}")
    except BridgeUnavailable as exc:
        log.warning("bridge unavailable: %s", exc)
    return True


def refresh_symbol_catalogue(bridge: BridgeClient, r=None) -> dict | None:
    """Discover the active account's broker symbols once its startup checks have passed, then every
    ``symbol_refresh_hours``. New symbols get the configured default scan flag (RR Trading One: off)."""
    r = r or get_redis()
    with session_scope() as db:
        st = tr.get_state(db)
        acct = tr.active_account(db)
        if acct is None or not st.startup_checks_passed:
            return None
        key = f"rr:symbols:refreshed:{acct.broker_connection_id}"
        if r.exists(key):
            return None
        try:
            res = tr.discover_symbols(db, bridge, acct.broker_connection_id)
        except BridgeError as exc:
            log.warning("symbol discovery failed: %s", exc)
            return None
        r.set(key, "1", ex=max(1, get_settings().symbol_refresh_hours) * 3600)
        log.info("symbol catalogue refreshed: %s", res)
        return res


# ---- analysis ---------------------------------------------------------------------------------------------------
def bridge_connected(r=None) -> bool:
    """The monitor publishes the bridge heartbeat; analysis only scans while it reports connected."""
    raw = (r or get_redis()).get("rr:bridge:health")
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("connected"))
    except (ValueError, AttributeError):
        return False


def analysis_cycle(bridge: BridgeClient, agent, should_stop=None) -> dict | None:
    if not bridge_connected():
        return None
    try:
        # should_stop: a stop request cancels the jobs not yet started (Phase 8 scheduler)
        res = (scan_once(bridge, agent, _noop_execute) if should_stop is None
               else scan_once(bridge, agent, _noop_execute, should_stop))
        log.info("scan: %s", res)
        return res
    except BridgeUnavailable as exc:
        log.warning("bridge unavailable: %s", exc)
        return None


# ---- trading ----------------------------------------------------------------------------------------------------
def pending_candidates(started_at: datetime, limit: int = TRADING_BATCH) -> list[int]:
    """BUY/SELL signals still NEW and unexpired, created after this trading process started (signals produced
    while the trading worker was stopped are never executed retroactively), best score first."""
    now = datetime.now(UTC)
    with session_scope() as db:
        return list(db.scalars(
            select(TradingSignal.id)
            .where(TradingSignal.status == "NEW", TradingSignal.decision.in_(("BUY", "SELL")),
                   TradingSignal.created_at >= started_at, TradingSignal.expires_at > now)
            .order_by(TradingSignal.score.desc(), TradingSignal.id)
            .limit(limit)))


def trading_cycle(auto, started_at: datetime) -> int:
    """Hand each candidate to the original auto-execution function. It re-checks auto_trading, the Claude
    review, the signal state, the Risk Engine and the dedupe key, so calling it twice is harmless."""
    ids = pending_candidates(started_at)
    for sid in ids:
        auto(sid)
    return len(ids)


# ---- process ----------------------------------------------------------------------------------------------------
def run(role: str) -> int:
    configure_logging()
    s = get_settings()
    r = get_redis()
    try:
        acquire(role, r)
    except RoleConflict as exc:
        log.error("%s: %s", role, exc)
        return 1
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    bridge: BridgeClient | None = None
    try:
        agent_activity(f"{role}_role", "processing", f"{role} role started (pid {os.getpid()})")
        started_at = datetime.now(UTC)
        last_ok, clock, last_scan, last_agent_check, agent_key = [True], {}, 0.0, 0.0, None
        agent = None
        if role == "analysis" and s.anthropic_api_key:
            from app.agents.claude_agent import ClaudeAgent
            agent = ClaudeAgent()
        auto = None
        while not stop["flag"]:
            r.expire(lock_key(role), LOCK_TTL)
            try:
                # (Re)connect: none registered yet, another agent selected, or the same agent re-linked (its
                # credentials rotated) — a stale secret would otherwise fail every request with "bad signature".
                if bridge is not None and time.time() - last_agent_check > 30:
                    last_agent_check = time.time()
                    if s.bridge_mode == "relay" and current_agent_key() != agent_key:
                        log.info("registered MT5 connector or its credentials changed; reconnecting")
                        bridge.close()
                        bridge, auto = None, None
                if bridge is None:
                    bridge = connect_bridge()
                    if bridge is None:
                        if role == "monitor":
                            publish_disconnected("no RR MT5 Connector agent is registered", r)
                        time.sleep(5)
                        continue
                    last_agent_check = time.time()
                    agent_key = current_agent_key()
                    auto = legacy.auto_execute_factory(bridge) if role == "trading" else None
                if role == "monitor":
                    monitor_cycle(bridge, last_ok, clock)
                    time.sleep(s.bridge_heartbeat_seconds)
                elif role == "analysis":
                    if time.time() - last_scan > s.scan_interval_seconds:
                        if analysis_cycle(bridge, agent, should_stop=lambda: stop["flag"]) is not None:
                            last_scan = time.time()
                    time.sleep(1)
                else:
                    trading_cycle(auto, started_at)
                    time.sleep(TRADING_POLL_SECONDS)
            except Exception:  # noqa: BLE001 - one failed cycle must not kill the role
                log.exception("%s cycle failed", role)
                time.sleep(2)
    finally:
        release(role, r)
        if bridge is not None:
            bridge.close()
        agent_activity(f"{role}_role", "waiting", f"{role} role stopped")
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ROLES:
        print(f"usage: python -m app.workers.roles {{{'|'.join(ROLES)}}}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(argv[0]))


if __name__ == "__main__":
    main()
