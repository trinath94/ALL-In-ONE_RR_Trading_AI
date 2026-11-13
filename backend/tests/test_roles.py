"""RR Trading One worker roles (decision D1): analysis never executes, the trading role only executes
Claude-confirmed signals created after it started (through the original auto-execution path), and roles cannot
run alongside the monolithic worker."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import redis
from sqlalchemy import func, select

from app.db.models import Symbol, TradeOrder, TradingSignal
from app.db.session import session_scope
from app.services import trading as tr
from app.workers import main as legacy
from app.workers import roles
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)

UTC = timezone.utc


@pytest.fixture
def r(db_clean):
    """The application's own Redis client (decoded responses), exactly as the roles use it in production."""
    c = roles.get_redis()
    c.flushdb()
    yield c
    c.flushdb()


def test_locks_work_with_raw_bytes_clients_too(db_clean):
    import os
    raw = redis.Redis.from_url(os.environ["RR_REDIS_URL"])
    raw.flushdb()
    roles.acquire("monitor", raw, owner="c1")
    roles.acquire("monitor", raw, owner="c1")
    with pytest.raises(roles.RoleConflict):
        roles.acquire("monitor", raw, owner="c2")
    roles.release("monitor", raw, owner="c1")
    assert not raw.exists(roles.lock_key("monitor"))


def make_signal(created_at: datetime, decision="BUY", review="BUY", score=80.0) -> int:
    with session_scope() as db:
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        sig = TradingSignal(symbol_id=sym.id, decision=decision, grade="A", timeframe="H1", entry=1.1000,
                            stop_loss=1.0980, tp1=1.1030, tp2=1.1050, risk_reward=2.5, score=score,
                            strategies=[{"strategy": "trend_ema_cross"}], ai_review={"decision": review},
                            expires_at=datetime.now(UTC) + timedelta(minutes=30), created_at=created_at)
        db.add(sig)
        db.flush()
        return sig.id


def enable_auto(on=True):
    with session_scope() as db:
        tr.get_state(db).auto_trading = on


def orders() -> int:
    with session_scope() as db:
        return db.scalar(select(func.count(TradeOrder.id)))


def test_role_locks(r):
    roles.acquire("monitor", r, owner="container-a")
    roles.acquire("analysis", r, owner="container-a")  # different roles coexist
    with pytest.raises(roles.RoleConflict, match="another 'monitor'"):
        roles.acquire("monitor", r, owner="container-b")
    roles.release("monitor", r, owner="container-b")   # not the owner: no effect
    assert r.exists(roles.lock_key("monitor"))
    roles.release("monitor", r, owner="container-a")
    roles.acquire("monitor", r, owner="container-b")
    with pytest.raises(ValueError):
        roles.acquire("everything", r)


def test_restarted_container_reclaims_its_own_stale_lock(r):
    roles.acquire("analysis", r, owner="container-a")  # previous incarnation was killed without releasing
    roles.acquire("analysis", r, owner="container-a")  # same container restarted by Docker: allowed
    assert r.ttl(roles.lock_key("analysis")) > 0


def test_no_registered_connector_means_wait_not_crash(r, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("RR_BRIDGE_MODE", "relay")
    get_settings.cache_clear()
    try:
        assert roles.connect_bridge() is None          # fresh install: no agent registered yet
        assert roles.current_agent_id() is None
    finally:
        monkeypatch.delenv("RR_BRIDGE_MODE")
        get_settings.cache_clear()
    roles.publish_disconnected("no RR MT5 Connector agent is registered", r)
    assert roles.bridge_connected(r) is False
    assert "no RR MT5 Connector" in json.loads(r.get("rr:bridge:health"))["error"]


def test_roles_refuse_to_run_alongside_monolithic_worker(r):
    r.set(legacy.LOCK_KEY, "1", ex=60)
    for role in roles.ROLES:
        with pytest.raises(roles.RoleConflict, match="monolithic worker"):
            roles.acquire(role, r)
    assert not any(r.exists(roles.lock_key(x)) for x in roles.ROLES)


def test_cli_rejects_unknown_role():
    with pytest.raises(SystemExit) as e:
        roles.main(["trade-everything"])
    assert e.value.code == 2


def test_bridge_connected_reads_monitor_heartbeat(r):
    assert roles.bridge_connected(r) is False
    r.set("rr:bridge:health", json.dumps({"connected": True}))
    assert roles.bridge_connected(r) is True
    r.set("rr:bridge:health", json.dumps({"connected": False, "error": "x"}))
    assert roles.bridge_connected(r) is False
    r.set("rr:bridge:health", "not json")
    assert roles.bridge_connected(r) is False


def test_analysis_never_executes_even_with_auto_trading_and_confirmed_signal(stack, monkeypatch):
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    enable_auto(True)
    sid = make_signal(datetime.now(UTC))  # an eligible, Claude-confirmed candidate exists
    seen = []
    real_scan = roles.scan_once

    def spy(b, agent, auto_execute):
        seen.append(auto_execute)
        res = real_scan(b, agent, auto_execute)
        auto_execute(sid)  # even if called with the eligible id, nothing may execute
        return res

    monkeypatch.setattr(roles, "scan_once", spy)
    legacy.heartbeat(bridge, [True])  # monitor publishes the connected heartbeat
    res = roles.analysis_cycle(bridge, None)
    assert res is not None and res["errors"] == 0
    assert seen == [roles._noop_execute]
    assert orders() == 0
    with session_scope() as db:
        assert db.get(TradingSignal, sid).status == "NEW"


def test_analysis_skips_when_monitor_reports_disconnected(stack, monkeypatch):
    called = []
    monkeypatch.setattr(roles, "scan_once", lambda *a: called.append(1))
    roles.get_redis().set("rr:bridge:health", json.dumps({"connected": False}))
    assert roles.analysis_cycle(stack[1], None) is None
    assert called == []


def test_trading_role_executes_only_new_confirmed_signals_once(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    enable_auto(True)
    started = datetime.now(UTC)
    old = make_signal(started - timedelta(minutes=5))            # produced while the worker was stopped
    unconfirmed = make_signal(started + timedelta(seconds=1), review="WAIT_FOR_CONFIRMATION", score=95)
    fresh = make_signal(started + timedelta(seconds=1))
    auto = legacy.auto_execute_factory(bridge)
    roles.trading_cycle(auto, started)
    with session_scope() as db:
        executed = {o.signal_id for o in db.scalars(select(TradeOrder))}
    assert executed == {fresh}
    assert old not in executed and unconfirmed not in executed
    roles.trading_cycle(auto, started)  # second pass: dedupe key + signal state prevent a duplicate
    assert orders() == 1
    assert len(fake.positions) == 1


def test_trading_role_respects_auto_trading_off(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    enable_auto(False)
    started = datetime.now(UTC)
    make_signal(started + timedelta(seconds=1))
    roles.trading_cycle(legacy.auto_execute_factory(bridge), started)
    assert orders() == 0 and not fake.positions


def test_monitor_cycle_runs_startup_checks_and_never_opens_positions(stack):
    fake, bridge = stack
    allow_unknown_news()
    enable_auto(True)
    make_signal(datetime.now(UTC))
    assert roles.monitor_cycle(bridge, [True], {"news": 1e18}) is True
    with session_scope() as db:
        assert tr.get_state(db).startup_checks_passed is True
    assert orders() == 0 and not fake.positions
