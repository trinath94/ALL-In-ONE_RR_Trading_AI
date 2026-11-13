"""Dynamic broker symbol catalogue (RR Trading One Phase 6): new symbols start scan-disabled when configured, user
choices survive rediscovery, symbols the broker stops offering are flagged (never deleted), scan scope controls never
enable excluded / missing / untradable symbols, the monitor refreshes the catalogue, and discovery errors are clean."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import BrokerConnection, Symbol
from app.db.session import session_scope
from app.execution.bridge import BridgeUnavailable
from app.services import trading as tr
from app.services.events import get_redis
from app.workers import main as worker
from app.workers import roles
from tests.test_api import h, login, make_user
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)


def syms() -> dict:
    with session_scope() as db:
        return {s.broker_symbol: s for s in db.scalars(select(Symbol))}


def rediscover(bridge):
    with session_scope() as db:
        return tr.discover_symbols(db, bridge, db.scalar(select(BrokerConnection.id)))


@pytest.fixture
def scan_off_by_default(monkeypatch):
    monkeypatch.setenv("RR_NEW_SYMBOLS_SCAN_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_original_default_is_unchanged(stack):
    assert get_settings().new_symbols_scan_enabled is True
    assert all(s.scan_enabled for s in syms().values()), "original behaviour: new symbols scan-enabled"


def test_new_symbols_start_disabled_and_user_choice_survives_rediscovery(stack, scan_off_by_default):
    fake, bridge = stack
    with session_scope() as db:
        db.execute(delete(Symbol))
    res = rediscover(bridge)
    assert res == {"discovered": 2, "new": 2, "missing": 0}
    s = syms()
    assert not s["EURUSD.r"].scan_enabled and not s["XAUUSD.r"].scan_enabled
    assert s["EURUSD.r"].last_seen_at is not None and s["EURUSD.r"].missing_since is None
    with session_scope() as db:
        db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r")).scan_enabled = True
    assert rediscover(bridge)["new"] == 0
    assert syms()["EURUSD.r"].scan_enabled and not syms()["XAUUSD.r"].scan_enabled


def test_symbol_no_longer_offered_is_flagged_not_deleted_and_recovers(stack):
    fake, bridge = stack
    gold = fake.symbols.pop("XAUUSD.r")
    res = rediscover(bridge)
    assert res["missing"] == 1
    x = syms()["XAUUSD.r"]
    assert x.missing_since is not None and x.tradable is False
    fake.symbols["XAUUSD.r"] = gold
    rediscover(bridge)
    x = syms()["XAUUSD.r"]
    assert x.missing_since is None and x.tradable is True


def test_specification_fields_recorded(stack):
    e = syms()["EURUSD.r"]
    assert float(e.tick_size) > 0 and float(e.tick_value) > 0 and float(e.contract_size) > 0
    assert float(e.volume_min) > 0 and float(e.volume_max) >= float(e.volume_min) and float(e.volume_step) > 0
    assert e.category == "forex" and syms()["XAUUSD.r"].category == "metals"


@pytest.fixture
def api(stack):
    from app.main import app
    make_user("trader1", "trader")
    make_user("viewer1", "readonly")
    with TestClient(app) as c:
        yield c, stack


def test_symbols_api_reports_catalogue_fields_honestly(api):
    c, _ = api
    assert login(c, "trader1").status_code == 200
    rows = {r["symbol"]: r for r in c.get("/api/symbols").json()}
    e = rows["EURUSD.r"]
    assert e["sessions"] == "not provided by the MT5 terminal API"
    assert e["trade_mode"] == "FULL" and e["eligible"] is True and e["excluded"] is False
    for k in ("tick_size", "tick_value", "volume_max", "spread_points", "last_seen_at", "missing_since"):
        assert k in e


def test_scan_scope_controls(api):
    c, (fake, bridge) = api
    make_user("admin2", "admin")      # same permission as the original per-symbol patch (strategy.manage)
    login(c, "admin2")
    ids = {r["symbol"]: r["id"] for r in c.get("/api/symbols").json()}
    r = c.post("/api/symbols/scan-scope", json={"enable": False, "scope": "all"}, headers=h(c))
    assert r.status_code == 200 and r.json()["changed"] == 2
    assert c.post("/api/symbols/scan-scope", json={"enable": True, "scope": "all"}, headers=h(c)).status_code == 422
    # exclude gold, then "enable all eligible" must skip it
    assert c.patch(f"/api/symbols/{ids['XAUUSD.r']}", json={"excluded": True}, headers=h(c)).status_code == 200
    r = c.post("/api/symbols/scan-scope", json={"enable": True, "scope": "all_eligible"}, headers=h(c)).json()
    assert r == {"changed": 1, "skipped": {"excluded": 1}}
    assert syms()["EURUSD.r"].scan_enabled and not syms()["XAUUSD.r"].scan_enabled
    assert c.patch(f"/api/symbols/{ids['XAUUSD.r']}", json={"scan_enabled": True}, headers=h(c)).status_code == 409
    # by asset class and by explicit symbols
    r = c.post("/api/symbols/scan-scope", json={"enable": False, "scope": "categories", "categories": ["forex"]}, headers=h(c))
    assert r.json()["changed"] == 1 and not syms()["EURUSD.r"].scan_enabled
    r = c.post("/api/symbols/scan-scope", json={"enable": True, "scope": "symbols", "symbol_ids": [ids["EURUSD.r"]]}, headers=h(c))
    assert r.json()["changed"] == 1
    # missing symbols are never enabled
    c.patch(f"/api/symbols/{ids['XAUUSD.r']}", json={"excluded": False}, headers=h(c))
    fake.symbols.pop("XAUUSD.r")
    rediscover(bridge)
    r = c.post("/api/symbols/scan-scope", json={"enable": True, "scope": "all_eligible"}, headers=h(c)).json()
    assert r["skipped"] == {"no_longer_offered": 1} and not syms()["XAUUSD.r"].scan_enabled
    # priorities
    assert c.patch(f"/api/symbols/{ids['EURUSD.r']}", json={"priority": 5}, headers=h(c)).status_code == 200
    assert syms()["EURUSD.r"].priority == 5


def test_scan_scope_requires_permission(api):
    c, _ = api
    for who in ("viewer1", "trader1"):
        login(c, who)
        r = c.post("/api/symbols/scan-scope", json={"enable": False, "scope": "all"}, headers=h(c))
        assert r.status_code == 403, who


def test_discovery_bridge_error_is_a_clear_502(api, monkeypatch):
    c, _ = api
    make_user("admin1", "admin")
    login(c, "admin1")

    def boom(*a, **k):
        raise BridgeUnavailable("connector offline")
    monkeypatch.setattr(tr, "discover_symbols", boom)
    r = c.post("/api/symbols/discover", headers=h(c))
    assert r.status_code == 502 and "MT5 connector unavailable" in r.json()["detail"]


def test_monitor_refreshes_catalogue_after_startup_checks(stack, scan_off_by_default):
    fake, bridge = stack
    r = get_redis()
    with session_scope() as db:
        db.execute(delete(Symbol))
    assert roles.refresh_symbol_catalogue(bridge, r) is None, "not before startup checks pass"
    assert worker.startup_checks(bridge)[0]
    res = roles.refresh_symbol_catalogue(bridge, r)
    assert res["discovered"] == 2 and res["new"] == 2
    assert all(not s.scan_enabled for s in syms().values())
    assert roles.refresh_symbol_catalogue(bridge, r) is None, "not again before the refresh interval"
    assert r.ttl([k for k in r.keys("rr:symbols:refreshed:*")][0]) > 3600
