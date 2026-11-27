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
from tests.test_mt5_agent import engine  # noqa: F401 (fixture)

UTC = timezone.utc
from tests.test_api import client  # noqa: E402,F401 (fixture)


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


def test_trading_role_never_executes_a_direction_the_review_did_not_confirm(stack):
    """Phase 15: a stored review in the other direction (the gateway's boundaries already refuse that) is still
    never executed, in either direction."""
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    enable_auto(True)
    started = datetime.now(UTC)
    make_signal(started + timedelta(seconds=1), decision="BUY", review="SELL")
    make_signal(started + timedelta(seconds=1), decision="SELL", review="BUY")
    roles.trading_cycle(legacy.auto_execute_factory(bridge), started)
    assert orders() == 0 and not fake.positions


def test_signals_from_while_auto_trading_was_off_are_never_executed_later(stack):
    """Phase 15 (found end-to-end): a candidate produced while auto trading was off stayed NEW and was executed as soon
    as auto trading was switched on. Only signals created after the switch are executed."""
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    started = datetime.now(UTC)
    while_off = make_signal(started + timedelta(seconds=1))
    switched_on = started + timedelta(seconds=2)
    with session_scope() as db:
        st = tr.get_state(db)
        st.auto_trading, st.auto_trading_since = True, switched_on
    after = make_signal(switched_on + timedelta(seconds=1))
    roles.trading_cycle(legacy.auto_execute_factory(bridge), started)
    with session_scope() as db:
        executed = {o.signal_id for o in db.scalars(select(TradeOrder))}
    assert executed == {after} and while_off not in executed


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


def test_relinking_rotates_the_credential_fingerprint(engine, tmp_path, monkeypatch):
    """Re-linking the same PC keeps the agent id but must change the key the roles reconnect on."""
    from tests.test_mt5_agent import make_manager, new_token
    m, *_ = make_manager(tmp_path)
    m.register(engine, new_token())
    k1 = roles.current_agent_key()
    m.register(engine, new_token())          # same device again: credentials rotate
    k2 = roles.current_agent_key()
    assert k1 and k2 and k1[0] == k2[0] and k1[1] != k2[1]
    assert roles.current_agent_id() == k1[0]


def test_switching_auto_trading_on_records_when(client):
    """Phase 15: the API records the OFF->ON switch; re-sending ON does not move it."""
    from tests.test_api import h, login
    from tests.test_trade_intelligence import accounts
    demo, _ = accounts()
    with session_scope() as db:
        from app.db.models import TradingAccount
        a = db.get(TradingAccount, demo)
        a.is_active, a.verified = True, True
    login(client, "admin")
    assert client.post("/api/control/auto-trading", json={"enabled": True}, headers=h(client)).status_code == 200
    with session_scope() as db:
        first = tr.get_state(db).auto_trading_since
    assert first is not None
    client.post("/api/control/auto-trading", json={"enabled": True}, headers=h(client))
    with session_scope() as db:
        assert tr.get_state(db).auto_trading_since == first
    client.post("/api/control/auto-trading", json={"enabled": False}, headers=h(client))
    client.post("/api/control/auto-trading", json={"enabled": True}, headers=h(client))
    with session_scope() as db:
        assert tr.get_state(db).auto_trading_since > first


def test_an_expired_lock_is_taken_back_and_a_long_cycle_keeps_it(r):
    """A cycle longer than the lock TTL (long scan, AI call, slow broker) let the lock expire, and it was never
    re-created: the role kept running but its health check failed for good and the desktop showed it stopped."""
    roles.acquire("analysis", r, owner="container-a")
    r.delete(roles.lock_key("analysis"))                      # the TTL ran out during a long cycle
    assert roles.renew("analysis", r, owner="container-a") and r.exists(roles.lock_key("analysis"))
    assert r.ttl(roles.lock_key("analysis")) > 0
    r.set(roles.lock_key("analysis"), "container-b")
    assert not roles.renew("analysis", r, owner="container-a"), "never takes a lock another process holds"
    assert roles._text(r.get(roles.lock_key("analysis"))) == "container-b"


def test_keepalive_renews_while_the_loop_progresses_and_stops_when_it_is_stuck(r):
    ka = roles.KeepAlive("trading", r, owner="container-a", every=0.05, stall=60)
    roles.acquire("trading", r, owner="container-a")
    r.delete(roles.lock_key("trading"))
    assert ka.tick() and r.exists(roles.lock_key("trading"))
    ka.start()
    try:
        r.delete(roles.lock_key("trading"))
        import time as _t
        deadline = _t.monotonic() + 3
        while not r.exists(roles.lock_key("trading")) and _t.monotonic() < deadline:
            _t.sleep(0.02)
        assert r.exists(roles.lock_key("trading")), "renewed in the background during a long cycle"
    finally:
        ka.stop()
    stuck = roles.KeepAlive("trading", r, owner="container-a", stall=0)
    r.delete(roles.lock_key("trading"))
    assert not stuck.tick() and not r.exists(roles.lock_key("trading")), "a stuck loop is still reported unhealthy"
