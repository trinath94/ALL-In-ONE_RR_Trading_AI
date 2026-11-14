"""AI learning and model validation (RR Trading One Phase 11, specification Part 11; Phase 1 gap 7.8): signed
artefacts that are never used unless authentic, walk-forward evaluation, comparison with the production model,
retraining recommendations from drift, and research through the AI gateway."""
import numpy as np
import pytest
from fastapi.testclient import TestClient  # noqa: F401
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Role, User
from app.db.models_intel import AIRecommendation, DriftEvent, ModelRegistry
from app.db.session import session_scope
from app.learning_engine import drift, models
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_intelligence import planted


@pytest.fixture
def signing(db_clean, monkeypatch):
    monkeypatch.setenv("RR_MODEL_SIGNING_KEY", "k" * 48)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def approver(db):
    u = User(username="approver11", password_hash="x" * 20, role_id=db.scalar(select(Role.id).where(Role.name == "admin")))
    db.add(u)
    db.flush()
    return u.id


def test_artefacts_are_signed_and_tampering_is_refused(signing):
    with session_scope() as db:
        good = models.train(db, planted(seed=1), horizon=10)
        assert good.status == "VALIDATED" and good.artifact_hmac and models.signature_state(good) == "valid"
        art = models.load(good)
        assert "model" in art
        # An attacker able to write the row replaces the artefact AND its SHA-256: integrity alone would accept it.
        evil = good.artifact + b"\x00"
        import hashlib
        good.artifact, good.artifact_sha256 = evil, hashlib.sha256(evil).hexdigest()
        assert models.signature_state(good) == "invalid"
        with pytest.raises(models.ModelIntegrityError):
            models.load(good)
        with pytest.raises(ValueError, match="signature invalid"):
            models.transition(db, good, "APPROVED", approver(db))


def test_unsigned_legacy_artefact_is_quarantined_from_use(signing):
    from app.intelligence import probability
    with session_scope() as db:
        m = models.train(db, planted(seed=5), horizon=10)
        m.artifact_hmac = None                                   # e.g. trained before Phase 11, or imported
        assert models.signature_state(m) == "unsigned"
        with pytest.raises(ValueError, match="unsigned"):
            models.transition(db, m, "APPROVED", approver(db))
        sig = {"entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12, "direction": "BUY", "strength": 0.5,
               "component": "trend", "strategy": "s"}
        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=300, freq="h", tz="UTC")
        df = pd.DataFrame({"open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1, "volume": 100}, index=idx)
        out = probability.estimate(db, df, sig, 1, 10, 0.00001, "EURUSD", "H1", record=False)
        assert out["available"] is False and out["status"] == "MODEL_INTEGRITY_FAILED", "never used, never estimated"


def test_without_a_key_the_original_sha256_behaviour_applies(db_clean, monkeypatch):
    monkeypatch.setenv("RR_MODEL_SIGNING_KEY", "")
    get_settings.cache_clear()
    try:
        with session_scope() as db:
            m = models.train(db, planted(seed=2), horizon=10)
            assert m.artifact_hmac is None and models.signature_state(m) == "not_enforced" and models.load(m)
    finally:
        get_settings.cache_clear()


def test_walk_forward_is_chronological_with_embargo_and_required(signing):
    with session_scope() as db:
        good = models.train(db, planted(seed=1), horizon=10)
        wf = good.metrics["walk_forward"]
        assert wf["passed"] and wf["scored"] >= 3 and wf["mean_brier_skill"] > 0 and wf["embargo_bars"] == 10
        for f in wf["folds"]:
            assert f["train_end"] < f["test_start"], "each fold trains strictly on the past"
        assert [f["train"] for f in wf["folds"]] == sorted(f["train"] for f in wf["folds"]), "expanding window"
        noise = models.train(db, planted(signal=False, seed=3), horizon=10)
        assert noise.status == "REJECTED" and not noise.metrics["walk_forward"]["passed"]
    # the effect-size floor: across 4 noise datasets x every candidate algorithm, walk-forward never passes
    for seed in (3, 4, 5, 6):
        d = planted(signal=False, seed=seed)
        for name, est in models.candidate_models().items():
            assert not models.walk_forward(d, "y_tp_before_sl", est, 10)["passed"], (seed, name)


def test_candidate_must_not_be_worse_than_production(signing):
    with session_scope() as db:
        uid = approver(db)
        prod = models.train(db, planted(seed=1), horizon=10)
        models.transition(db, prod, "APPROVED", uid)
        models.transition(db, prod, "PRODUCTION", uid)
        ds = planted(seed=9)
        X, y = ds[models.FEATURES].astype(float), ds["y_tp_before_sl"].to_numpy()
        flat = models.compare_with_production(db, prod.model_key, X, y, np.full(len(y), y.mean()))
        assert flat["production"].startswith("v1") and flat["passed"] is False, "an uninformative candidate is worse"
        nxt = models.train(db, planted(seed=2), horizon=10)
        bc = nxt.metrics["baseline_comparison"]
        assert bc["production"].startswith("v1") and "brier_production" in bc
        assert (nxt.status == "VALIDATED") == bc["passed"] or not nxt.metrics["walk_forward"]["passed"]


def test_rollback_refuses_an_unusable_target(signing):
    with session_scope() as db:
        uid = approver(db)
        v1 = models.train(db, planted(seed=1), horizon=10)
        v2 = models.train(db, planted(seed=2), horizon=10)
        for v in (v1, v2):
            models.transition(db, v, "APPROVED", uid)
            models.transition(db, v, "PRODUCTION", uid)
        v1.artifact_hmac = "0" * 64
        with pytest.raises(ValueError, match="rollback refused"):
            models.rollback(db, v2.model_key, uid)
        assert v2.status == "PRODUCTION"


def test_critical_drift_recommends_retraining_once(db_clean):
    with session_scope() as db:
        ev = [DriftEvent(scope="feature", subject="prob_tp_before_sl:v3", metric="psi:rsi", value=0.4, threshold=0.1,
                         severity="critical"),
              DriftEvent(scope="feature", subject="prob_tp_before_sl:v3", metric="psi:adx14", value=0.12, threshold=0.1,
                         severity="warning"),
              DriftEvent(scope="performance", subject="DEMO:smc_bos", metric="expectancy_last_window", value=-3,
                         threshold=0, severity="critical")]
        assert drift.recommend_retraining(db, ev) == ["prob_tp_before_sl:v3"], "models only, not strategies"
        db.flush()
        assert drift.recommend_retraining(db, ev) == [], "not duplicated while open"
        rec = db.scalar(select(AIRecommendation).where(AIRecommendation.kind == "RETRAIN"))
        assert rec.status == "OPEN" and rec.evidence["drift"] == [{"scope": "feature", "metric": "psi:rsi", "value": 0.4,
                                                                    "threshold": 0.1}]


def test_learner_research_goes_through_the_gateway(db_clean, monkeypatch):
    from cryptography.fernet import Fernet

    from app.ai import gateway
    from app.agents.strategy_discovery import agent as research_agent
    from app.learning_engine import scheduler
    from tests.test_ai_gateway import Scripted, provider, route
    monkeypatch.setenv("RR_AI_KEYS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RR_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert scheduler.job_discovery() == {"skipped": "no AI provider is configured for strategy discovery"}
        route("strategy_discovery", (provider("local"), "llama3"))
        prop = {k: "x" for k in ("name", "category", "concept", "mathematical_foundation", "market_conditions",
                                 "entry_conditions", "exit_conditions", "stop_loss_logic", "take_profit_logic",
                                 "implementation_complexity", "testing_method")}
        prop.update({k: [] for k in ("timeframes", "required_data", "advantages", "weaknesses", "risks",
                                     "closest_existing_strategies")})
        prop.update(name="Kalman velocity breakout", concept="state-space trend estimate")
        fake = Scripted({"local": [{"gaps": ["g"], "weaknesses": [], "duplicates": [], "proposals": [prop]}]})
        monkeypatch.setattr(gateway, "_provider", lambda hop, factory: fake.factory(hop))
        with session_scope() as db:
            out = research_agent.research(db, {"strategy": {}})
        assert out["gaps"] == ["g"] and len(out["proposals"]) == 1 and fake.seen[0]["provider"] == "local"
    finally:
        get_settings.cache_clear()


def test_model_api_reports_signature_and_refuses_promotion(client, signing):
    with session_scope() as db:
        m = models.train(db, planted(seed=1), horizon=10)
        m.artifact_hmac = None
        mid = m.id
    login(client, "admin")
    row = next(x for x in client.get("/api/learning/models").json() if x["id"] == mid)
    assert row["signature"] == "unsigned" and row["metrics"]["walk_forward"]["passed"]
    r = client.post(f"/api/learning/models/{mid}/transition", json={"to": "APPROVED"}, headers=h(client))
    assert r.status_code == 409 and "unsigned" in r.text
    with session_scope() as db:
        assert db.get(ModelRegistry, mid).status == "VALIDATED"


def test_cached_model_is_reverified_when_its_signature_changes(signing):
    """A model loaded once must not stay usable from the cache after its signature is removed or replaced."""
    import pandas as pd

    from app.intelligence import probability
    with session_scope() as db:
        m = models.train(db, planted(seed=6), horizon=10)
        sig = {"entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12, "direction": "BUY", "strength": 0.5,
               "component": "trend", "strategy": "s"}
        idx = pd.date_range("2026-01-01", periods=300, freq="h", tz="UTC")
        df = pd.DataFrame({"open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1, "volume": 100}, index=idx)
        assert probability.estimate(db, df, sig, 1, 10, 0.00001, "EURUSD", "H1", record=False)["available"]
        for forged in (None, "0" * 64):
            m.artifact_hmac = forged
            out = probability.estimate(db, df, sig, 1, 10, 0.00001, "EURUSD", "H1", record=False)
            assert out["available"] is False and out["status"] == "MODEL_INTEGRITY_FAILED", forged
