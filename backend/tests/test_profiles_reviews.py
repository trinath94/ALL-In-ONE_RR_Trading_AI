"""Adaptive configuration profiles (versioned, validated, rollback, never changing the strategy), AI proposals (bounded,
untrusted), the automatic promotion policy (DEMO only, off by default), the structured pre-trade analysis and the
post-trade review with the normal-loss vs recurring-defect test."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.claude_agent import REVIEW_SCHEMA  # noqa: F401
from app.db.models import AuditLog, ClosedPosition, Strategy, StrategyExperiment, StrategyPerformance, TradingAccount
from app.db.models_opportunity import AutomationPolicy, Opportunity, StrategyConfigProfile
from app.db.session import session_scope
from app.learning_engine import review
from app.strategies import profiles, registry
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_part29 import bars

CODE = "trend_ema_9_21"
SPEC = {"symbol": "EURUSD", "timeframe": "H1", "point": 0.00001, "tick_size": 0.00001, "tick_value": 1.0,
        "spread_points": 10}


def mk(db, params=None, **kw):
    return profiles.create(db, CODE, params or {"fast": 8}, **kw)


# ---- parameters and versions ------------------------------------------------------------------------------------------
def test_parameters_are_checked_and_ai_values_are_bounded(db_clean):
    assert profiles.check_params(CODE, {"fast": 8.4}) == {"fast": 8}          # integer default stays integer
    for bad in ({"nope": 1}, {"fast": "8"}, {"fast": True}, {}, {"sl_atr": -1}):
        with pytest.raises(profiles.ProfileError):
            profiles.check_params(CODE, bad)
    assert profiles.check_params(CODE, {"sl_atr": 3.0}, bounded=True) == {"sl_atr": 3.0}
    with pytest.raises(profiles.ProfileError):
        profiles.check_params(CODE, {"sl_atr": 3.1}, bounded=True)           # > 2x the default
    with pytest.raises(profiles.ProfileError):
        profiles.check_params("no_such_strategy", {"x": 1})


def test_versions_parents_and_the_strategy_is_never_changed(db_clean):
    before = dict(registry.get(CODE).params)
    with session_scope() as db:
        a = mk(db)
        b = mk(db, {"fast": 7})
        c = mk(db, {"fast": 10}, regime="STRONG_BULL")
        assert (a.version, b.version, c.version) == (1, 2, 3)
        assert a.parent_id is None and b.parent_id == a.id and c.parent_id is None, "parents follow the same regime"
        assert a.status == "PROPOSED" and a.history[0]["to"] == "PROPOSED"
        with pytest.raises(profiles.ProfileError):
            mk(db, regime="MOON")
    assert dict(registry.get(CODE).params) == before and len(registry.all_strategies()) == 215


# ---- validation gates -------------------------------------------------------------------------------------------------
def test_real_validation_records_every_gate(db_clean):
    with session_scope() as db:
        p = mk(db)
        v = profiles.validate(db, p, bars(1500), SPEC)
        assert set(v["gates"]) == {"sample_size", "expectancy", "profit_factor", "drawdown", "stability", "walk_forward",
                                   "cost_sensitivity", "bias"}
        assert all("detail" in g for g in v["gates"].values())
        assert p.status == ("VALIDATED" if v["passed"] else "REJECTED")
        assert v["passed"] == all(g["passed"] for g in v["gates"].values())
        assert [x["to"] for x in p.history] == ["PROPOSED", "VALIDATING", p.status]


def fake_eval(monkeypatch, prof_oos, base_oos, prof_is=None, wf_ev=1.0, cost_ev=1.0, bias=True):
    m = lambda **k: {"total_trades": 50, "win_rate": 0.5, "profit_factor": 1.5, "expected_value": 2.0,  # noqa: E731
                     "max_drawdown_pct": 5.0, "net_profit": 100.0, **k}

    def side(df, code, params, spec, regime):
        if spec.get("spread_points") == 20:
            return {"in_sample": m(), "out_of_sample": m(expected_value=cost_ev)}
        if params == dict(registry.get(CODE).params):
            return {"in_sample": m(), "out_of_sample": m(**base_oos)}
        return {"in_sample": m(**(prof_is or {})), "out_of_sample": m(**prof_oos)}
    monkeypatch.setattr(profiles, "_side", side)
    import app.backtesting.engine as bt
    monkeypatch.setattr(bt, "walk_forward", lambda *a, **k: {"windows": [1, 2, 3], "combined_oos": {"expected_value": wf_ev}})
    import app.strategies.experimental.variants as var
    monkeypatch.setattr(var, "bias_report", lambda *a, **k: {"passed": bias, "look_ahead": {"detail": "x"}})


@pytest.mark.parametrize("case,expect_fail", [
    (dict(prof_oos={}, base_oos={"expected_value": 1.0}), None),
    (dict(prof_oos={"total_trades": 12}, base_oos={}), "sample_size"),
    (dict(prof_oos={"expected_value": 0.5}, base_oos={"expected_value": 1.0}, prof_is={"expected_value": 0.6}),
     "expectancy"),
    (dict(prof_oos={"profit_factor": 1.02}, base_oos={}), "profit_factor"),
    (dict(prof_oos={"max_drawdown_pct": 9.0}, base_oos={"max_drawdown_pct": 5.0}), "drawdown"),
    (dict(prof_oos={"expected_value": 2.0}, base_oos={"expected_value": 1.0}, prof_is={"expected_value": 9.0}), "stability"),
    (dict(prof_oos={}, base_oos={}, wf_ev=-0.1), "walk_forward"),
    (dict(prof_oos={}, base_oos={}, cost_ev=-0.5), "cost_sensitivity"),
    (dict(prof_oos={}, base_oos={}, bias=False), "bias"),
])
def test_each_overfitting_gate_can_reject(db_clean, monkeypatch, case, expect_fail):
    fake_eval(monkeypatch, **case)
    with session_scope() as db:
        p = mk(db)
        v = profiles.validate(db, p, bars(300), SPEC)
        failed = [k for k, g in v["gates"].items() if not g["passed"]]
        if expect_fail is None:
            assert v["passed"] and p.status == "VALIDATED", failed
        else:
            assert failed == [expect_fail] and p.status == "REJECTED"


# ---- activation, LIVE, rollback ----------------------------------------------------------------------------------------
def validated(db, params=None, **kw):
    p = mk(db, params, **kw)
    p.validation = {"passed": True}
    p.status = "VALIDATED"
    return p


def test_activation_rules_and_rollback(db_clean, monkeypatch):
    with session_scope() as db:
        p = mk(db)
        with pytest.raises(profiles.ProfileError):
            profiles.transition(db, p, "DEMO_ACTIVE", 1)          # not validated
        with pytest.raises(profiles.ProfileError):
            profiles.transition(db, p, "VALIDATED", 1)            # only validation sets it
        v1 = validated(db, {"fast": 8})
        profiles.transition(db, v1, "DEMO_ACTIVE", None, "auto")
        v2 = validated(db, {"fast": 7})
        profiles.transition(db, v2, "DEMO_ACTIVE", 1)
        assert v1.status == "RETIRED" and v2.status == "DEMO_ACTIVE", "one active profile per scope"
        with pytest.raises(profiles.ProfileError):
            profiles.transition(db, v2, "LIVE_ACTIVE", None)      # never automatic
        with pytest.raises(profiles.ProfileError):
            profiles.transition(db, v2, "LIVE_ACTIVE", 1)         # no DEMO forward evidence
        monkeypatch.setattr(profiles, "forward_stats", lambda db, p, mode="DEMO": {"trades": 25, "net": 40.0, "win_rate": 0.6})
        profiles.transition(db, v2, "LIVE_ACTIVE", 1)
        assert v2.status == "LIVE_ACTIVE" and v2.validation["forward_demo"]["trades"] == 25
        back = profiles.rollback(db, v2, 1)
        assert v2.status == "RETIRED" and back is not None and back.id == v1.id and v1.status == "LIVE_ACTIVE"
        assert profiles.rollback(db, v1, 1) is None and v1.status == "RETIRED", "no parent: the defaults apply"


def test_apply_picks_the_most_specific_active_profile(db_clean):
    with session_scope() as db:
        generic = validated(db, {"fast": 8})
        bull = validated(db, {"fast": 12}, regime="STRONG_BULL")
        live = validated(db, {"slow": 30})
        profiles.transition(db, generic, "DEMO_ACTIVE", 1)
        profiles.transition(db, bull, "DEMO_ACTIVE", 1)
        live.status = "LIVE_ACTIVE"
        enabled = {CODE: {"rr": 2.0}, "other": {}}
        out, applied = profiles.apply(db, enabled, "DEMO", "STRONG_BULL", "EURUSD.r", "H1")
        assert out[CODE] == {"rr": 2.0, "fast": 12} and applied == {CODE: f"{bull.id}:v{bull.version}"}
        out, applied = profiles.apply(db, enabled, "DEMO", "SIDEWAYS", "EURUSD.r", "H1")
        assert out[CODE]["fast"] == 8, "the DEMO profile under test wins the tie with the LIVE one"
        out, applied = profiles.apply(db, enabled, "LIVE", "SIDEWAYS", "EURUSD.r", "H1")
        assert out[CODE] == {"rr": 2.0, "slow": 30}, "LIVE uses LIVE_ACTIVE profiles only"
        sym_only = validated(db, {"fast": 5}, symbols=["XAUUSD.r"])
        profiles.transition(db, sym_only, "DEMO_ACTIVE", 1)
        out, _ = profiles.apply(db, enabled, "DEMO", "SIDEWAYS", "EURUSD.r", "H1")
        assert out[CODE]["fast"] == 8, "a symbol-scoped profile does not apply to another symbol"
        assert out["other"] == {} and enabled[CODE] == {"rr": 2.0}, "the input is not modified"


def test_the_scan_uses_active_profiles_and_records_the_version(scan, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    allow_unknown_news()
    from app.workers import main as worker
    from app.scanner.service import scan_once
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        codes = [c for c, cls in registry.all_strategies().items() if dict(cls.params)][:215]
        made = {}
        for c in codes:
            k, v = next(iter(dict(registry.get(c).params).items()))
            p = profiles.create(db, c, {k: v})
            p.validation, p.status = {"passed": True}, "DEMO_ACTIVE"
            made[c] = f"{p.id}:v{p.version}"
    scan_once(bridge, None, lambda sid: None)
    from app.db.models import TradingSignal
    with session_scope() as db:
        entries = [e for s in db.scalars(select(TradingSignal)) for e in (s.strategies or [])]
    tagged = [e for e in entries if e.get("config_profile")]
    assert tagged and all(e["config_profile"] == made[e["strategy"]] for e in tagged)


# ---- AI proposals and the promotion policy ----------------------------------------------------------------------------
def test_ai_proposal_is_bounded_untrusted_and_only_proposed(keys):
    a = provider("prop")
    route("config_proposal", (a, "m"))
    good = {"params": {"fast": 12, "sl_atr": 2.0}, "regime": "STRONG_BULL", "reason": "trend regime evidence"}
    with session_scope() as db:
        p = profiles.propose_ai(db, CODE, None, provider_factory=Scripted({"prop": [good]}).factory)
        assert p.status == "PROPOSED" and p.source == "AI" and p.ai_provider.startswith("prop/")
        assert p.params == {"fast": 12, "sl_atr": 2.0} and p.regime == "STRONG_BULL"
    for bad in ({**good, "params": {"fast": 40}}, {**good, "params": {"lot": 5}}):
        with session_scope() as db:
            with pytest.raises(profiles.ProfileError):
                profiles.propose_ai(db, CODE, None, provider_factory=Scripted({"prop": [bad]}).factory)


def test_promotion_policy_is_off_by_default_and_never_reaches_live(db_clean):
    with session_scope() as db:
        v = validated(db)
        assert profiles.auto_promote(db) == {"profiles": 0, "experiments": 0} and v.status == "VALIDATED"
        profiles.policy(db).auto_promote_profiles_demo = True
        assert profiles.auto_promote(db)["profiles"] == 1 and v.status == "DEMO_ACTIVE"
        assert profiles.auto_promote(db)["profiles"] == 0 and v.status == "DEMO_ACTIVE", "never further than DEMO"
        db.add(StrategyExperiment(code="exp_policy_test", title="x", hypothesis="x", stage="VALIDATION", proposal={},
                                  validation={}, evidence={}))
        db.flush()
        profiles.policy(db).auto_promote_experiments_demo = True
        assert profiles.auto_promote(db)["experiments"] == 1
        e = db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == "exp_policy_test"))
        assert e.stage == "DEMO_TESTING"


def test_profile_and_policy_api(client):
    login(client, "admin")
    r = client.post(f"/api/strategies/{CODE}/profiles", json={"params": {"nope": 1}}, headers=h(client))
    assert r.status_code == 400
    r = client.post(f"/api/strategies/{CODE}/profiles", json={"params": {"fast": 8}, "regime": "SIDEWAYS",
                                                              "reason": "test"}, headers=h(client))
    assert r.status_code == 200 and r.json()["status"] == "VALIDATING"
    pid = r.json()["id"]
    act = lambda **b: client.post(f"/api/profiles/{pid}/action", json=b, headers=h(client))  # noqa: E731
    assert act(action="activate_demo").status_code == 409                       # not validated
    with session_scope() as db:
        p = db.get(StrategyConfigProfile, pid)
        p.status, p.validation = "DEMO_ACTIVE", {"passed": True}
    assert act(action="activate_live").status_code == 428                       # phrase
    assert act(action="activate_live", confirm_text="ACTIVATE LIVE PROFILE").status_code == 409   # no forward evidence
    assert act(action="rollback").json()["status"] == "RETIRED"
    lst = client.get(f"/api/strategies/{CODE}/profiles").json()
    assert lst["defaults"]["fast"] == 9 and lst["profiles"][0]["id"] == pid
    assert client.get("/api/automation/policy").json()["auto_promote_profiles_demo"] is False
    assert client.put("/api/automation/policy", json={"auto_promote_profiles_demo": True},
                      headers=h(client)).json()["auto_promote_profiles_demo"] is True
    with session_scope() as db:
        acts = {a.action for a in db.scalars(select(AuditLog))}
    assert {"PROFILE_CREATE", "PROFILE_ROLLBACK", "AUTOMATION_POLICY"} <= acts
    login(client, "trader")
    assert client.put("/api/automation/policy", json={"auto_promote_profiles_demo": False},
                      headers=h(client)).status_code == 403
    assert client.post(f"/api/strategies/{CODE}/profiles", json={"params": {"fast": 8}},
                       headers=h(client)).status_code == 403


# ---- pre-trade analysis -----------------------------------------------------------------------------------------------
def test_pre_trade_analysis_from_the_decision_trace(scan, monkeypatch, client):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    allow_unknown_news()
    from app.scanner.service import scan_once
    from app.workers import main as worker
    assert worker.startup_checks(bridge)[0]
    scan_once(bridge, None, lambda sid: None)
    with session_scope() as db:
        o = db.scalars(select(Opportunity).where(Opportunity.direction == "BUY").order_by(Opportunity.id)).first()
        oid = o.id
    login(client, "viewer")
    t = client.get(f"/api/opportunities/{oid}/thesis").json()
    for k in ("why", "supporting_strategies", "favourable_conditions", "contradicting_conditions", "invalidation",
              "entry_justification", "stop_justification", "target_justification", "failure_risks", "should_reject"):
        assert k in t, k
    assert t["direction"] == "BUY"
    # the test forces BUY whatever the strategies said: supporting ones are exactly those signalling BUY, and any
    # strategy pointing the other way is listed as a contradiction
    assert all(x["direction"] == "BUY" for x in t["supporting_strategies"])
    assert t["why"].startswith(f"{len(t['supporting_strategies'])} strategies signal BUY")
    assert t["should_reject"] == bool(t["reject_reasons"])
    assert client.get("/api/opportunities/999999/thesis").status_code == 404


# ---- post-trade review: normal loss vs recurring defect ---------------------------------------------------------------
def test_binomial_tail():
    assert review.binomial_tail(0, 10, 0.5) == pytest.approx(1.0)
    assert review.binomial_tail(10, 10, 0.5) == pytest.approx(1 / 1024)
    assert review.binomial_tail(6, 10, 0.5) == pytest.approx(386 / 1024)


def seed(code, outcomes, mode="DEMO"):
    now = datetime.now(UTC)
    with session_scope() as db:
        acct = db.scalar(select(TradingAccount).where(TradingAccount.mode == mode))
        base = 700000 + db.query(ClosedPosition).count()
        for i, win in enumerate(outcomes):
            db.add(ClosedPosition(account_id=acct.id, ticket=base + i, symbol="EURUSD.r", direction="BUY", volume=0.1,
                                  open_price=1.1, close_price=1.101 if win else 1.099, profit=10.0 if win else -10.0,
                                  opened_at=now - timedelta(hours=200 - i), closed_at=now - timedelta(hours=199 - i),
                                  close_reason="TP" if win else "STOP_LOSS", mode=mode, strategy_codes=[code]))


def evidence(code, trades, wins):
    with session_scope() as db:
        sid = db.scalar(select(Strategy.id).where(Strategy.code == code))
        db.add(StrategyPerformance(strategy_id=sid, source="OOS", trades=trades, wins=wins,
                                   gross_profit=float(wins), gross_loss=float(trades - wins)))


def test_losses_within_variance_are_normal_and_excess_losses_are_flagged(stack):
    evidence(CODE, 100, 55)                                       # expected loss rate 45 %
    seed(CODE, [True] * 16 + [False] * 14)                        # 14/30 losses: normal
    with session_scope() as db:
        r = review.loss_significance(db, "DEMO", CODE)
    assert r["verdict"] == "normal_variance" and r["p_value"] > 0.05, r
    seed(CODE, [True] * 6 + [False] * 24)                         # last 30: 24 losses vs 45 % expected
    with session_scope() as db:
        r = review.loss_significance(db, "DEMO", CODE)
    assert r["verdict"] == "recurring_defect_suspected" and r["p_value"] < 0.05, r
    with session_scope() as db:
        assert review.loss_significance(db, "DEMO", "trend_macd_cross")["verdict"] == "insufficient_evidence"


def test_review_answers_every_question_and_is_stored_by_the_learner(stack):
    evidence(CODE, 100, 55)
    seed(CODE, [True, False] * 15)
    from app.learning_engine import failure, features
    with session_scope() as db:
        features.enrich_pending(db, None)
        failure.run(db, "DEMO")
    from app.db.models_intel import FailureReport
    with session_scope() as db:
        reps = db.scalars(select(FailureReport)).all()
        assert reps and all(r.review for r in reps)
        loss = next(r.review for r in reps if r.outcome == "LOSS")
        assert set(loss["questions"]) == {"strategy_appropriate", "entry_correct", "regime_identified", "stop_suitable",
                                          "exit_appropriate", "execution_acceptable", "prediction_matched",
                                          "profitable_after_costs"}
        assert loss["verdict"] in ("normal_loss", "mistake", "recurring_defect_suspected")
        assert loss["questions"]["profitable_after_costs"]["ok"] is False
        assert loss["loss_significance"][0]["strategy"] == CODE
        win = next(r.review for r in reps if r.outcome == "WIN")
        assert win["verdict"] == "win" and not win["loss_significance"]
        cid = reps[0].closed_position_id
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.test_api import make_user
    make_user("viewer2", "readonly")
    c = TestClient(app)
    login(c, "viewer2")
    assert c.get(f"/api/trades/{cid}/review").json()["review"]["verdict"]
    assert c.get(f"/api/strategies/{CODE}/loss-significance").json()["strategy"] == CODE
