"""Multi-symbol / multi-timeframe scanning (RR Trading One Phase 8): once per closed bar, timeframe compatibility
outside the strategy classes, configured peer data, budget / timeout / cancellation, outcome recording, scanner
settings and status, peer configuration."""
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db.models import AnalysisResult, AuditLog, ScannerSettings, Symbol
from app.db.session import session_scope
from app.scanner import peers as peer_sugg
from app.scanner import scheduler as sched
from app.scanner.service import EVAL_KEY, scan_once
from app.services.events import get_redis
from app.strategies import registry
from tests.test_api import h, login, make_user
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)


# ---- pure scheduling -------------------------------------------------------------------------------------------
def test_due_only_for_a_new_closed_bar():
    assert sched.is_due(None, 1000) and sched.is_due(1000, 4600)
    assert not sched.is_due(4600, 4600) and not sched.is_due(4600, 1000)
    assert sched.is_due(4600, None), "unknown bar: analyse, so a problem is reported instead of silently skipped"


def test_order_priority_then_oldest_waiting_then_higher_timeframe():
    jobs = [sched.Job(priority=100, bar_epoch=7200, tf="H1", symbol_id=1, symbol="B"),
            sched.Job(priority=100, bar_epoch=3600, tf="H1", symbol_id=2, symbol="C"),   # waiting longer
            sched.Job(priority=10, bar_epoch=9000, tf="M15", symbol_id=3, symbol="A"),   # higher priority
            sched.Job(priority=100, bar_epoch=0, tf="H4", symbol_id=4, symbol="D")]      # unknown bar: first in tier
    assert [j.symbol for j in sched.order(jobs)] == ["A", "D", "C", "B"]


def _report():
    return sched.CycleReport(started_at="t", timeframes=["H1"], symbols=1)


def test_budget_defers_the_rest_and_stop_cancels():
    jobs = [sched.Job(priority=1, bar_epoch=i, tf="H1", symbol_id=i, symbol=f"S{i}") for i in range(1, 9)]
    rep = _report()
    out = sched.run_jobs(jobs, lambda j: (time.sleep(0.3), {"symbol": j.symbol, "tf": j.tf})[1], max_workers=2,
                         budget_s=0.5, job_timeout_s=10, should_stop=lambda: False, report=rep)
    assert rep.analysed == len(out) and 2 <= rep.analysed <= 6 and rep.deferred == 8 - rep.analysed
    stop = threading.Event()
    rep2 = _report()

    def work(j):
        stop.set()
        return {"symbol": j.symbol, "tf": j.tf}
    sched.run_jobs(jobs, work, max_workers=1, budget_s=30, job_timeout_s=10, should_stop=stop.is_set, report=rep2)
    assert rep2.cancelled >= 6 and rep2.analysed <= 2


def test_slow_job_is_abandoned_not_waited_for():
    gate = threading.Event()
    jobs = [sched.Job(priority=1, bar_epoch=1, tf="H1", symbol_id=1, symbol="SLOW"),
            sched.Job(priority=2, bar_epoch=1, tf="H1", symbol_id=2, symbol="FAST")]

    def work(j):
        if j.symbol == "SLOW":
            gate.wait(5)
        return {"symbol": j.symbol, "tf": j.tf}
    rep = _report()
    t0 = time.monotonic()
    sched.run_jobs(jobs, work, max_workers=2, budget_s=30, job_timeout_s=0.5, should_stop=lambda: False, report=rep)
    gate.set()
    assert time.monotonic() - t0 < 3 and rep.timed_out == 1 and rep.analysed == 1
    assert "timed out" in rep.error_samples[0]


# ---- scanning through the bridge -------------------------------------------------------------------------------
def varied(fake, shift_hours=0):
    base = fake.copy_rates_from_pos

    def rates(name, tf, start, count):
        arr = base(name, tf, start, count)
        rng = np.random.default_rng(abs(hash(name)) % 1000)
        steps = np.cumsum(rng.normal(0, 0.0008, count)) * arr["close"][0]
        arr["close"] = arr["close"] + steps
        arr["open"] = np.r_[arr["close"][0], arr["close"][:-1]]
        arr["high"] = np.maximum(arr["open"], arr["close"]) * 1.0004
        arr["low"] = np.minimum(arr["open"], arr["close"]) * 0.9996
        arr["time"] = arr["time"] - 3600 * 24 + 3600 * shift_hours[0]   # all bars closed; shift = new bar
        return arr
    fake.copy_rates_from_pos = rates


def analyses() -> int:
    with session_scope() as db:
        return db.scalar(select(func.count(AnalysisResult.id)))


def set_peers(name, peers):
    with session_scope() as db:
        db.scalar(select(Symbol).where(Symbol.broker_symbol == name)).peers = peers


def evals(code) -> dict:
    out = {}
    for day_key in get_redis().scan_iter(EVAL_KEY.format(day="*")):
        for k, v in get_redis().hgetall(day_key).items():
            k = k.decode() if isinstance(k, bytes) else k
            if k.startswith(code + "|"):
                out[k.split("|")[1]] = out.get(k.split("|")[1], 0) + int(v)
    return out


@pytest.fixture
def scan(stack):
    fake, bridge = stack
    shift = [0]
    varied(fake, shift)
    yield fake, bridge, shift
    with session_scope() as db:
        db.execute(delete(ScannerSettings))


def test_each_closed_bar_is_analysed_once(scan):
    fake, bridge, shift = scan
    first = scan_once(bridge, None, lambda _id: None)
    jobs = 2 * 3                                                    # 2 symbols x M15,H1,H4
    assert first["analyses"] == jobs and first["not_due"] == 0 and analyses() == jobs
    again = scan_once(bridge, None, lambda _id: None)
    assert again["analyses"] == 0 and again["not_due"] == jobs and analyses() == jobs, "same bars: nothing new"
    shift[0] = 1                                                    # the broker closes a new bar
    third = scan_once(bridge, None, lambda _id: None)
    assert third["analyses"] == jobs and analyses() == 2 * jobs
    st = get_redis().get(sched.STATUS_KEY)
    assert st and '"analysed": 6' in st


def test_strategies_run_only_on_declared_timeframes(scan):
    fake, bridge, _ = scan
    h1_only = next(c for c, cls in registry.all_strategies().items()
                   if "M15" not in cls.timeframes and "H1" in cls.timeframes and not cls.requires and not cls.risky)
    scan_once(bridge, None, lambda _id: None)
    e = evals(h1_only)
    assert e.get("not_applicable", 0) == 2, f"{h1_only} on M15 for 2 symbols: {e}"   # never evaluated on M15
    assert e.get("signal", 0) + e.get("no_signal", 0) >= 2
    with session_scope() as db:
        m15 = db.scalar(select(AnalysisResult).where(AnalysisResult.timeframe == "M15"))
        assert m15.structure["evaluation"]["not_applicable"] > 0
        assert m15.structure["data_caps"] == ["tick_volume"]


def test_configured_peers_supply_peer_strategies(scan):
    fake, bridge, shift = scan
    scan_once(bridge, None, lambda _id: None)
    assert registry.get("ict_smt").timeframes == ["M15", "H1", "H4"]
    assert evals("ict_smt") == {"unavailable": 6}, "no peers configured: unavailable on 3 timeframes x 2 symbols"
    set_peers("EURUSD.r", ["XAUUSD.r"])
    shift[0] = 1
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        a = db.scalar(select(AnalysisResult).where(AnalysisResult.symbol_id == sym.id, AnalysisResult.timeframe == "H1")
                      .order_by(AnalysisResult.id.desc()))
        assert a.structure["peers"] == ["XAUUSD.r"] and "peer_symbols" in a.structure["data_caps"]
        assert "ict_smt" not in a.structure["unavailable"]
    e = evals("ict_smt")
    assert e.get("signal", 0) + e.get("no_signal", 0) >= 1, e


def test_peer_no_longer_offered_is_reported_not_used(scan):
    fake, bridge, _ = scan
    set_peers("EURUSD.r", ["XAUUSD.r"])
    with session_scope() as db:
        from datetime import UTC, datetime
        db.scalar(select(Symbol).where(Symbol.broker_symbol == "XAUUSD.r")).missing_since = datetime.now(UTC)
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        a = db.scalar(select(AnalysisResult).where(AnalysisResult.symbol_id == sym.id, AnalysisResult.timeframe == "H1"))
        assert a.structure["peers"] == [] and "not offered" in a.structure["peer_problems"][0]
        assert "ict_smt" in a.structure["unavailable"]


def test_job_already_running_elsewhere_is_not_started_twice(scan):
    fake, bridge, _ = scan
    with session_scope() as db:
        eur = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r")).id
    get_redis().set(sched.inflight_key(eur, "H1"), "1", ex=60)
    res = scan_once(bridge, None, lambda _id: None)
    assert res["analyses"] == 5
    assert '"in_flight_elsewhere": 1' in get_redis().get(sched.STATUS_KEY)


# ---- API -----------------------------------------------------------------------------------------------------
@pytest.fixture
def api(scan):
    from app.main import app
    make_user("admin8", "admin")
    make_user("viewer8", "readonly")
    with TestClient(app) as c:
        yield c, scan


def test_scanner_settings_and_status(api):
    c, (fake, bridge, shift) = api
    login(c, "viewer8")
    assert c.put("/api/scanner/settings", json={"timeframes": ["H1"]}, headers=h(c)).status_code == 403
    login(c, "admin8")
    r = c.put("/api/scanner/settings", json={"timeframes": ["W1"]}, headers=h(c))
    assert r.status_code == 422 and "W1" in r.text
    r = c.put("/api/scanner/settings", json={"timeframes": ["h1", "M15", "H1"]}, headers=h(c))
    assert r.status_code == 200 and r.json() == {"timeframes": ["M15", "H1"]}
    res = scan_once(bridge, None, lambda _id: None)
    assert res["analyses"] == 4, "2 symbols x the 2 chosen timeframes"
    st = c.get("/api/scanner/status").json()
    assert st["settings"]["timeframes"] == ["M15", "H1"] and st["settings"]["source"] == "console"
    assert st["last_cycle"]["analysed"] == 4 and st["last_cycle"]["timeframes"] == ["M15", "H1"]
    assert {j["timeframe"] for j in st["jobs"]} == {"M15", "H1"} and all(j["last_bar"] for j in st["jobs"])
    app_ = {a["timeframe"]: a for a in st["applicability"]}
    assert app_["M15"]["applicable"] < app_["M15"]["enabled"] and app_["D1"]["applicable"] < app_["H1"]["applicable"]
    with session_scope() as db:
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "SCANNER_SETTINGS")) == 1


def test_peer_configuration_is_validated_and_suggestions_are_explicit(api):
    c, _ = api
    login(c, "admin8")
    ids = {r["symbol"]: r["id"] for r in c.get("/api/symbols").json()}
    assert c.patch(f"/api/symbols/{ids['EURUSD.r']}", json={"peers": ["EURUSD.r"]}, headers=h(c)).status_code == 422
    assert c.patch(f"/api/symbols/{ids['EURUSD.r']}", json={"peers": ["GBPUSD.x"]}, headers=h(c)).status_code == 422
    assert c.patch(f"/api/symbols/{ids['EURUSD.r']}", json={"peers": ["XAUUSD.r"]}, headers=h(c)).status_code == 200
    # a broker that also offers GBPUSD: suggested, but only applied on request
    with session_scope() as db:
        eur = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        db.add(Symbol(broker_connection_id=eur.broker_connection_id, broker_symbol="GBPUSD.r", canonical="GBPUSD",
                      category="forex", point=eur.point, tick_size=eur.tick_size, tick_value=eur.tick_value,
                      contract_size=eur.contract_size, volume_min=eur.volume_min, volume_max=eur.volume_max,
                      volume_step=eur.volume_step))
    rows = {r["symbol"]: r for r in c.get("/api/symbols").json()}
    assert rows["GBPUSD.r"]["suggested_peers"] == ["EURUSD.r"] and rows["GBPUSD.r"]["peers"] == []
    assert rows["EURUSD.r"]["suggested_peers"] == ["GBPUSD.r"] and rows["EURUSD.r"]["peers"] == ["XAUUSD.r"]
    r = c.post("/api/symbols/peers/apply-suggested", headers=h(c)).json()
    assert r["changed"] == [{"symbol": "GBPUSD.r", "peers": ["EURUSD.r"]}], "configured symbols are left alone"
    login(c, "viewer8")
    assert c.post("/api/symbols/peers/apply-suggested", headers=h(c)).status_code == 403


def test_library_reports_evaluations(api):
    c, (fake, bridge, _) = api
    scan_once(bridge, None, lambda _id: None)
    login(c, "viewer8")
    rows = {r["code"]: r for r in c.get("/api/strategies").json()}
    smc = rows["smc_bos"]["evaluations"]
    assert sum(smc.values()) == 6 and smc == {**smc, **{k: v for k, v in evals("smc_bos").items()}}
    s = c.get("/api/strategies/summary").json()
    assert s["evaluations"]["not_applicable"] > 0 and sum(s["evaluations"].values()) > 0


def test_peer_suggestions_never_include_inverse_or_self():
    offered = {"EURUSD": "EURUSD.r", "USDCHF": "USDCHF.r", "GBPUSD": "GBPUSD.r", "XAUUSD": "XAUUSD"}
    assert peer_sugg.suggest("EURUSD", offered) == ["GBPUSD.r"]
    assert peer_sugg.suggest("USDCHF", offered) == []
    assert peer_sugg.suggest("XAUUSD", offered) == []          # XAGUSD not offered
