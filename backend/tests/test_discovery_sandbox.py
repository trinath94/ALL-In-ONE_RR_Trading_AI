"""Part 25 discovery/experimental pipeline + safety tests."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import StrategyExperiment
from app.db.models_intel import ApprovalRecord, LearningJob
from app.db.session import session_scope
from app.learning_engine import scheduler
from app.strategies import registry
from app.strategies.experimental import lifecycle, sandbox
from app.strategies.experimental.validator import validate
from tests.synthetic import make_ohlc
from tests.test_api import h, login, make_user

GOOD = open("app/strategies/experimental/library/exp_cusum_changepoint.py").read()

MALICIOUS = [
    ("import os\nclass X(Strategy):\n    code, name, category = 'exp_x1', 'x', 'experimental'\n    def detect(self, ctx):\n        return None\n", "import of 'os'"),
    ("class X(Strategy):\n    code, name, category = 'exp_x2', 'x', 'experimental'\n    def detect(self, ctx):\n        return eval('1')\n", "eval"),
    ("class X(Strategy):\n    code, name, category = 'exp_x3', 'x', 'experimental'\n    def detect(self, ctx):\n        return ctx.__class__.__bases__\n", "dunder"),
    ("class X(Strategy):\n    code, name, category = 'exp_x4', 'x', 'experimental'\n    def detect(self, ctx):\n        return open('/etc/passwd')\n", "open"),
    ("class X(Strategy):\n    code, name, category = 'prod_x', 'x', 'experimental'\n    def detect(self, ctx):\n        return None\n", "exp_"),
]


@pytest.mark.parametrize("src,needle", MALICIOUS)
def test_static_validator_rejects_unsafe_code(src, needle):
    v = validate(src)
    assert not v["ok"] and any(needle in e for e in v["errors"])
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_job("unit", src)


def test_runtime_import_guard_and_resource_limits():
    # passes static checks but tries a dynamic import inside the sandbox -> blocked by the runtime hook
    src = GOOD.replace("import numpy as np", "import numpy as np\nimport statistics").replace(
        "    def detect(self, ctx):", "    def detect(self, ctx):\n        import math\n        math.sqrt(4)")
    assert validate(src)["ok"]
    assert sandbox.run_job("unit", src)["passed"]
    hog = ("class Hog(Strategy):\n    code, name, category = 'exp_hog', 'h', 'experimental'\n    min_bars = 10\n"
           "    def detect(self, ctx):\n        x = 0\n        while True:\n            x += 1\n")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_job("unit", hog, timeout=20, cpu_seconds=3)


def test_experimental_code_never_reaches_production_registry():
    before = set(registry.all_strategies())
    sandbox.run_job("unit", GOOD)
    assert set(registry.all_strategies()) == before
    assert not any(c.startswith("exp_") for c in registry.all_strategies())
    with pytest.raises(KeyError):
        registry.get("exp_cusum_changepoint")


def test_lifecycle_gates_versions_and_rollback(db_clean):
    with session_scope() as db:
        assert lifecycle.register_library(db)["registered"] == 4
        assert lifecycle.register_library(db)["registered"] == 0
        e = db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == "exp_cusum_changepoint"))
        assert e.stage == "DEVELOPING" and e.version == 1 and e.duplicate_of is not None
        with pytest.raises(ValueError, match="unit tests"):
            lifecycle.transition(db, e, "EXPERIMENTAL", None)
        lifecycle.run_unit(db, e)
        assert e.stage == "EXPERIMENTAL"
        with pytest.raises(ValueError):
            lifecycle.transition(db, e, "DEMO_TESTING", None)          # cannot skip stages
        lifecycle.transition(db, e, "BACKTESTING", None)
        with pytest.raises(ValueError, match="validation"):
            lifecycle.transition(db, e, "VALIDATION", None)
        e.stage = "DEMO_TESTING"
        with pytest.raises(ValueError, match="forward"):
            lifecycle.transition(db, e, "APPROVAL_REQUIRED", None)
        e.validation = {**e.validation, "forward": {"resolved": 25}}
        lifecycle.transition(db, e, "APPROVAL_REQUIRED", None)
        with pytest.raises(PermissionError):
            lifecycle.transition(db, e, "APPROVED", None)              # approval needs a user
        # new source version resets evidence; rollback restores the previous source
        lifecycle.attach_source(db, e, GOOD.replace('"threshold": 4.0', '"threshold": 4.5'), None, "tweak")
        assert e.version == 2 and e.stage == "DEVELOPING" and "unit" not in e.validation
        lifecycle.rollback(db, e, 1)
        assert '"threshold": 4.0' in e.source_code and e.version == 3
        assert db.scalar(select(func.count(ApprovalRecord.id)).where(ApprovalRecord.entity_id == e.id)) >= 4


def test_validation_pipeline_reports_and_protects_final_test(db_clean):
    df = make_ohlc(2400, seed=7, freq="1h")
    with session_scope() as db:
        lifecycle.register_library(db)
        e = db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == "exp_cusum_changepoint"))
        lifecycle.run_unit(db, e)
        rep = lifecycle.run_validation(db, e, df, {"spread_points": 10})
        for key in ("development", "validation", "monte_carlo_validation", "cost_sensitivity", "slippage_sensitivity",
                    "walk_forward_folds", "benchmark", "checks", "n_trials_for_multiple_testing"):
            assert key in rep
        if not rep["validation_passed"]:
            assert rep["final_test"] is None and e.stage == "REJECTED"   # final set untouched
        else:
            assert rep["final_test"] is not None


def test_forward_shadow_test_never_sends_orders(db_clean):
    df = make_ohlc(800, seed=9, freq="1h")
    with session_scope() as db:
        lifecycle.register_library(db)
        e = db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == "exp_hurst_regime"))
        out = lifecycle.forward_step(db, e, "SYN", df)
        assert set(out) == {"open", "resolved"}
        assert "no orders" in e.validation["forward"]["note"]
        from app.db.models import TradeOrder
        assert db.scalar(select(func.count(TradeOrder.id))) == 0


def test_scheduler_idempotency_retry_and_timeout(db_clean):
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return {"x": 1}

    job = scheduler.Job("t_ok", 3600, 5, ok)
    assert scheduler.run_job(job)["status"] == "DONE"
    assert scheduler.run_job(job)["skipped"] is True            # same period -> not run twice
    assert calls["n"] == 1

    def boom():
        raise RuntimeError("nope")
    bad = scheduler.Job("t_bad", 3600, 5, boom)
    for _ in range(4):
        scheduler.run_job(bad)
    with session_scope() as db:
        row = db.scalar(select(LearningJob).where(LearningJob.job_type == "t_bad"))
        assert row.status == "FAILED" and row.attempts == scheduler.MAX_ATTEMPTS

    import time

    def slow():
        time.sleep(3)
        return {}
    assert scheduler.run_job(scheduler.Job("t_slow", 3600, 1, slow))["status"] == "TIMEOUT"


def test_discovery_agent_flags_duplicates_with_stubbed_claude(db_clean):
    from app.agents.strategy_discovery import agent as disc
    from tests.test_agent_scanner import stub_client
    from app.agents.claude_agent import ClaudeAgent
    prop = {k: ([] if k in ("timeframes", "required_data", "advantages", "weaknesses", "risks", "closest_existing_strategies") else "x")
            for k in disc.PROPOSAL_FIELDS}
    dup = {**prop, "name": "EMA 9/21 Crossover", "concept": "EMA crossover fast slow trend", "entry_conditions": "ema 9 crosses ema 21"}
    new = {**prop, "name": "Wavelet denoised momentum", "concept": "multi-scale wavelet energy shift", "entry_conditions": "energy ratio"}
    client, _ = stub_client({"gaps": ["g"], "weaknesses": [], "duplicates": [], "proposals": [dup, new]})
    with session_scope() as db:
        out = disc.research(db, {}, agent=ClaudeAgent(client=client))
        flags = {p["name"]: p["duplicate"] for p in out["proposals"]}
        assert flags["Wavelet denoised momentum"] is False
        stages = {e.title: e.stage for e in db.scalars(select(StrategyExperiment))}
        assert stages["Wavelet denoised momentum"] == "DISCOVERED"
    client2, _ = stub_client({"source": GOOD.replace("exp_cusum_changepoint", "exp_wavelet_momentum"), "notes": "n"})
    with session_scope() as db:
        e = db.scalar(select(StrategyExperiment).where(StrategyExperiment.title == "Wavelet denoised momentum"))
        res = disc.develop(db, e, agent=ClaudeAgent(client=client2))
        assert res["static"]["ok"] and e.stage == "DEVELOPING" and e.code == "exp_wavelet_momentum"


# ---------------------------------------------------------------- API & permissions ----------------
@pytest.fixture
def client(db_clean):
    from app.main import app
    make_user("admin", "admin")
    make_user("trader", "trader")
    make_user("viewer", "readonly")
    return TestClient(app)


def test_learning_and_intelligence_endpoints_respond(client):
    login(client, "viewer")
    for path in ("/api/learning/overview", "/api/learning/performance", "/api/learning/performance/correlation",
                 "/api/learning/trades", "/api/learning/memory", "/api/learning/failures", "/api/learning/recommendations",
                 "/api/learning/jobs", "/api/learning/experiments", "/api/learning/models", "/api/learning/drift",
                 "/api/learning/approvals", "/api/intelligence/regimes", "/api/intelligence/consensus",
                 "/api/intelligence/consensus/analytics", "/api/intelligence/probability", "/api/intelligence/anomalies",
                 "/api/intelligence/portfolio", "/api/intelligence/execution", "/api/intelligence/fingerprints",
                 "/api/intelligence/adjustments"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:200])
    assert client.get("/api/learning/overview").json()["overall"]["verdict"] == "NO_DATA"
    assert "Unavailable" in client.get("/api/intelligence/probability").json()["status"]


def test_readonly_and_trader_cannot_promote_models_or_experiments(client):
    with session_scope() as db:
        lifecycle.register_library(db)
        eid = db.scalar(select(StrategyExperiment.id).limit(1))
    login(client, "viewer")
    assert client.post(f"/api/learning/experiments/{eid}/unit", headers=h(client)).status_code == 403
    assert client.post("/api/learning/jobs/drift/run", headers=h(client)).status_code == 403
    login(client, "trader")
    assert client.post("/api/learning/models/1/transition", json={"to": "PRODUCTION"}, headers=h(client)).status_code == 403
    login(client, "admin")
    r = client.post(f"/api/experiments/{eid}/stage", json={"stage": "ADMIN_APPROVAL"}, headers=h(client))
    assert r.status_code == 409                                     # legacy name accepted, gate enforced
    r = client.post(f"/api/learning/experiments/{eid}/source", json={"source": "import os\n" + "x" * 60}, headers=h(client))
    assert r.status_code == 200 and r.json()["static"]["ok"] is False
_ = (SimpleNamespace, json)
