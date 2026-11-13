"""Strategy library (RR Trading One Phase 7): real registry metadata, honest data status, the scanner's effective
selection per mode, baseline integrity, activity from stored analysis results only, performance kept apart by source,
and risky strategies never reaching the LIVE scan set."""
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import AnalysisResult, Strategy, StrategyConfiguration, StrategyPerformance, Symbol
from app.db.session import session_scope
from app.scanner.service import enabled_strategies
from app.services import strategy_library as lib
from tests.test_api import client, h, login, make_user  # noqa: F401 (fixture)
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)


@pytest.fixture
def restore_strategies():
    """strategies / strategy_configurations / strategy_performance are not truncated between tests: restore."""
    with session_scope() as db:
        before = {s.code: s.enabled for s in db.scalars(select(Strategy))}
    yield
    with session_scope() as db:
        for s in db.scalars(select(Strategy)):
            s.enabled = before[s.code]
        db.execute(delete(StrategyConfiguration))
        db.execute(delete(StrategyPerformance))


def by_code(c) -> dict:
    return {r["code"]: r for r in c.get("/api/strategies").json()}


def test_risky_strategy_enabled_for_demo_never_scans_live(client, restore_strategies):
    login(client, "admin")
    assert client.patch("/api/strategies/quant_martingale", json={"enabled": True, "mode": "DEMO"},
                        headers=h(client)).status_code == 200
    # saving LIVE parameters creates a LIVE configuration row; it must still not reach the LIVE scan set
    assert client.patch("/api/strategies/quant_martingale", json={"params": {}, "mode": "LIVE"},
                        headers=h(client)).status_code == 200
    with session_scope() as db:
        assert "quant_martingale" in enabled_strategies(db, "DEMO")
        assert "quant_martingale" not in enabled_strategies(db, "LIVE")
    r = by_code(client)["quant_martingale"]
    assert r["scanned"] == {"DEMO": True, "LIVE": False}


def test_non_risky_live_selection_unchanged(client, restore_strategies):
    with session_scope() as db:
        demo, live = enabled_strategies(db, "DEMO"), enabled_strategies(db, "LIVE")
    risky = {"quant_grid", "quant_martingale", "quant_anti_martingale", "quant_dca"}
    assert set(demo) - risky == set(live) and not risky & set(live)


def test_library_lists_registry_with_honest_data_status(client):
    login(client, "viewer")
    rows = client.get("/api/strategies").json()
    assert len(rows) == 215
    counts = {}
    for r in rows:
        counts[r["data_status"]] = counts.get(r["data_status"], 0) + 1
        for k in ("timeframes", "regimes", "requires", "risky", "code_hash", "min_bars", "params", "data_notes"):
            assert k in r
    # Phase 1 audit: 195 runnable on a standard MT5 feed; 5 peer strategies run where peers are configured (Phase 8);
    # 6 need engine-side data not supplied; 9 need data MT5 cannot provide
    assert counts == {"runnable": 195, "per_symbol": 5, "not_supplied": 6, "unsupported": 9}
    r = {x["code"]: x for x in rows}
    assert r["ict_smt"]["data_status"] == "per_symbol" and "configured peers" in r["ict_smt"]["data_notes"][0]
    assert r["vol_footprint"]["data_status"] == "unsupported"
    assert r["sess_news"]["data_status"] == "not_supplied"
    # nothing is invented: no trades means no performance and zero observed activity
    assert all(x["performance"] is None and x["performance_by_source"] == {} for x in rows)
    assert all(x["activity"] == {"signals": 0, "errors": 0, "last_signal_at": None} for x in rows)
    assert all(x["baseline"] == "unchanged" for x in rows)


def test_summary_matches_registry_facts(client):
    login(client, "viewer")
    s = client.get("/api/strategies/summary").json()
    assert s["total"] == s["expected_total"] == 215
    assert all(c["count"] == c["expected"] for c in s["categories"])
    assert s["baseline"]["unchanged"] == 215 and s["baseline"]["modified"] == [] and s["baseline"]["baseline_count"] == 215
    assert s["timeframes"] == {"M1": 2, "M5": 33, "M15": 204, "M30": 7, "H1": 176, "H4": 176, "D1": 6, "W1": 0}
    assert s["timeframes_enforced"] is True and "only on the timeframes it declares" in s["timeframes_note"]
    assert s["risky"] == ["quant_anti_martingale", "quant_dca", "quant_grid", "quant_martingale"]
    assert {d["status"]: d["count"] for d in s["data_status"]} == {"runnable": 195, "per_symbol": 5,
                                                                     "not_supplied": 6, "unsupported": 9}
    assert s["activity"]["analyses"] == 0


def test_baseline_detects_modification_and_missing_inventory(client, tmp_path, monkeypatch):
    base = json.load(open(lib.baseline_path()))
    base["strategies"]["smc_bos"]["code_hash"] = "0" * 16
    p = tmp_path / "inv.json"
    p.write_text(json.dumps(base))
    monkeypatch.setenv("RR_STRATEGY_BASELINE_PATH", str(p))
    get_settings.cache_clear()
    try:
        assert lib.baseline_status()["smc_bos"] == "modified"
        login(client, "viewer")
        assert client.get("/api/strategies/summary").json()["baseline"]["modified"] == ["smc_bos"]
        monkeypatch.setenv("RR_STRATEGY_BASELINE_PATH", str(tmp_path / "absent.json"))
        get_settings.cache_clear()
        assert client.get("/api/strategies/summary").json()["baseline"] is None
        assert by_code(client)["smc_bos"]["baseline"] == "baseline inventory not available"
    finally:
        get_settings.cache_clear()


def test_activity_and_performance_come_from_stored_records(stack, restore_strategies):
    from app.main import app
    make_user("viewer7", "readonly")
    with session_scope() as db:
        sym = db.scalar(select(Symbol))
        now = datetime.now(UTC)
        sig = {"strategy": "smc_bos", "direction": "BUY", "entry": 1.1}
        err = {"strategy": "pa_pin_bar", "status": "error", "reason": "ValueError()"}
        for outputs in ([sig, err], [sig]):
            db.add(AnalysisResult(symbol_id=sym.id, timeframe="H1", bar_time=now, regime="TREND", structure={},
                                  strategy_outputs=outputs, score={}))
        sid = db.scalar(select(Strategy.id).where(Strategy.code == "smc_bos"))
        db.add(StrategyPerformance(strategy_id=sid, source="OOS", trades=10, wins=6, gross_profit=5, gross_loss=3))
        db.add(StrategyPerformance(strategy_id=sid, source="WALKFORWARD", trades=4, wins=1, gross_profit=1, gross_loss=2))
    with TestClient(app) as c:
        login(c, "viewer7")
        r = by_code(c)
        assert r["smc_bos"]["activity"]["signals"] == 2 and r["smc_bos"]["activity"]["errors"] == 0
        assert r["smc_bos"]["activity"]["last_signal_at"] is not None
        assert r["pa_pin_bar"]["activity"] == {"signals": 0, "errors": 1, "last_signal_at": None}
        assert r["smc_bos"]["performance_by_source"] == {"OOS": {"trades": 10, "wins": 6},
                                                          "WALKFORWARD": {"trades": 4, "wins": 1}}
        s = c.get("/api/strategies/summary").json()["activity"]
        assert s == {"days": 7, "analyses": 2, "strategies_with_signals": 1, "strategies_with_errors": ["pa_pin_bar"]}


def test_library_is_read_only_for_viewers(client):
    login(client, "viewer")
    assert client.patch("/api/strategies/smc_bos", json={"enabled": False}, headers=h(client)).status_code == 403
