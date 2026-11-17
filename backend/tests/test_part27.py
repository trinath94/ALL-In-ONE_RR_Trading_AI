"""Parts 27-30 Phase 4 (Part 27): opportunity lifecycle and decision trace, the five new specialist agents, extended
regime tags, the data-source registry, event-driven re-analysis, the OpenAI preset and the dual-provider consensus
review. Simulated broker (FakeMT5 through the real bridge) and scripted AI providers only."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.claude_agent import AgentError, Review
from app.ai import gateway
from app.ai.providers import PRESETS, OpenAICompatibleProvider
from app.analysis import regime as rg
from app.core.config import get_settings
from app.db.models import AnalysisResult, EconomicEvent, RiskEvaluation, Symbol, TradingSignal
from app.db.models_ai import AgentRun
from app.db.models_opportunity import Opportunity, OpportunityTransition
from app.db.session import session_scope
from app.intelligence import opportunity as opx
from app.risk.engine import RiskParams
from app.scanner import triggers
from app.scanner.service import expire_signals, scan_once
from app.services.events import get_redis
from tests.test_ai_gateway import REVIEW_OK, Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, login, make_user  # noqa: F401 (fixture)
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_scanner_scheduling import varied

NEW_AGENTS = {"fundamental_analysis", "entry_validation", "exit_optimisation", "failure_prediction"}


def res(decision="BUY", direction="BUY", entry=1.1000, sl=1.0950, tp1=1.1050, tp2=1.1100, tp3=None, strategies=None):
    return SimpleNamespace(decision=decision, direction=direction, entry=entry, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                           grade="A", score=80.0, strategies=strategies or [{"strategy": "s1", "direction": direction}],
                           invalidation=["close below 1.0950"], expires_at=datetime(2026, 11, 17, tzinfo=UTC))


# ---- deterministic specialist views ----------------------------------------------------------------------------------
def test_entry_validation_geometry_fails_and_price_checks_wait():
    kw = {"max_chase_atr": 0.5, "max_spread_sl": 0.25}
    ok = opx.entry_validation(res(), 1.1002, 0.0001, 0.0040, **kw)
    assert ok["outcome"] == "PASS", ok
    chased = opx.entry_validation(res(), 1.1040, 0.0001, 0.0040, **kw)            # 1 ATR away from the entry
    assert chased["outcome"] == "WAIT" and "entry_not_chased" in chased["reason"]
    wide = opx.entry_validation(res(), 1.1000, 0.0020, 0.0040, **kw)              # spread 40% of the stop distance
    assert wide["outcome"] == "WAIT" and "spread_vs_stop" in wide["reason"]
    wrong = opx.entry_validation(res(sl=1.1050), 1.1000, 0.0001, 0.0040, **kw)    # BUY with the stop above entry
    assert wrong["outcome"] == "FAIL" and "stop_on_correct_side" in wrong["reason"]
    assert opx.entry_validation(res("NO_TRADE", None), 1.1, 0.0, 0.004, **kw)["outcome"] == "N/A"
    # the engine's WAIT may have a direction but no levels yet: not a defect (it stays WAIT)
    assert opx.entry_validation(res("WAIT", "SELL", entry=None, sl=None), 1.1, 0.0, 0.004, **kw)["outcome"] == "N/A"
    assert opx.entry_validation(res(entry=None), 1.1, 0.0, 0.004, **kw)["outcome"] == "FAIL"
    state, why = opx.initial_state(res(), wrong)
    assert state == "REJECTED" and why.startswith("entry validation failed: stop_on_correct_side")


def test_exit_plan_uses_levels_and_the_risk_engines_own_parameters():
    plan = opx.exit_plan(res(), RiskParams())
    assert plan["available"] and [t["r_multiple"] for t in plan["targets"]] == [1.0, 2.0]
    assert plan["order_take_profit"] == "tp2"
    assert plan["management"] == {"break_even_at_r": 1.0, "partial_close_at_r": 1.0, "partial_close_fraction": 0.5,
                                  "trailing_start_r": 1.5, "trailing_atr_mult": 2.0}
    assert opx.exit_plan(res("WAIT", None), RiskParams())["available"] is False


def test_failure_prediction_names_its_factors_and_never_claims_low_without_history():
    stats = {"s1": {"trades": 10, "wins": 6}}
    thin = opx.failure_prediction(res(), stats, {}, "CLEAR", [], 1)
    assert thin["level"] == "MEDIUM" and not thin["evidence_sufficient"]
    assert [f["factor"] for f in thin["factors"]] == ["insufficient_history"]
    good = opx.failure_prediction(res(), {"s1": {"trades": 100, "wins": 60}}, {}, "CLEAR", [], 1)
    assert good["level"] == "LOW" and good["factors"] == []
    intel = {"anomalies": {"found": [{"kind": "flash_move", "severity": "warning", "explanation": "x"}]},
             "consensus": {"decision": "SELL"}, "probability": {"available": True, "tp_before_sl_probability": 0.3}}
    bad = opx.failure_prediction(res(), {"s1": {"trades": 100, "wins": 30}}, intel, "RESTRICTED", ["UNCERTAIN"], -1)
    names = {f["factor"] for f in bad["factors"]}
    assert bad["level"] == "HIGH" and {"weak_history", "anomaly_flash_move", "news_window", "regime_uncertain",
                                       "against_higher_timeframe", "low_model_probability", "consensus_disagrees"} <= names
    assert "not a probability" in bad["method"]


def test_fundamental_view_is_unavailable_without_a_calendar(db_clean):
    with session_scope() as db:
        v = opx.fundamental_view(db, None, "EURUSD")
    assert v == {"available": False, "reason": "economic calendar not configured or stale"}


# ---- regime tags -------------------------------------------------------------------------------------------------------
def frame(close, high_pad=0.0004, low_pad=0.0004, vol=None):
    idx = pd.date_range("2026-01-01", periods=len(close), freq="h", tz="UTC")
    c = pd.Series(close, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + high_pad, "low": np.minimum(o, c) - low_pad, "close": c,
                         "volume": vol if vol is not None else 100.0})


def test_extra_regime_tags_are_added_and_the_primary_label_is_unchanged():
    rng = np.random.default_rng(1)
    calm = 1.1 + np.cumsum(rng.normal(0, 0.0002, 300))
    wild = calm.copy()
    wild[-10:] += np.cumsum(rng.normal(0, 0.004, 10))
    df = frame(wild)
    r = rg.classify(df)
    assert r.primary in rg.REGIMES and "VOLATILITY_EXPANSION" in r.tags
    a14 = rg.ind.atr(df)
    assert rg.extra_tags(df, a14, r.primary, r.ema_slope, r.structure_trend) == \
        [t for t in r.tags if t in rg.EXTRA_TAGS]
    # accumulation: a range whose bars close near their highs on volume
    flat = frame(np.full(60, 1.1), vol=np.full(60, 100.0))
    flat["low"] = flat["close"] - 0.0010
    flat["high"] = flat["close"] + 0.0001
    assert "ACCUMULATION" in rg.extra_tags(flat, rg.ind.atr(flat), "SIDEWAYS", 0.0, 0)
    flat["low"], flat["high"] = flat["close"] - 0.0001, flat["close"] + 0.0010
    assert "DISTRIBUTION" in rg.extra_tags(flat, rg.ind.atr(flat), "CONSOLIDATION", 0.0, 0)
    assert "UNCERTAIN" in rg.extra_tags(flat, rg.ind.atr(flat), "SIDEWAYS", 0.5, -1)
    # no strategy declares the new tags, so suitability matching is unaffected
    from app.strategies import registry
    declared = {x for m in registry.all_strategies().values() for x in (m.regimes or [])}
    assert not declared & set(rg.EXTRA_TAGS)


# ---- lifecycle ---------------------------------------------------------------------------------------------------------
def test_lifecycle_allows_only_forward_transitions():
    assert set(opx.ALLOWED) == set(opx.STATES) | {None}
    for t in opx.TERMINAL:
        assert opx.ALLOWED[t] == frozenset()
    assert "EXECUTED" not in opx.ALLOWED["RESEARCHING"], "nothing executes without passing the review stage first"
    opp = SimpleNamespace(id=1, state="REJECTED")
    with pytest.raises(opx.TransitionError):
        opx.transition(SimpleNamespace(add=lambda _: None), opp, "QUALIFIED", "x")


# ---- the scan: opportunities, trace, agent runs, API, source registry ------------------------------------------------
@pytest.fixture
def scan(stack):
    fake, bridge = stack
    shift = [0]
    varied(fake, shift)
    yield fake, bridge, shift


def force_buy(monkeypatch, entry_atr_offset=0.0):
    """The real engine rarely emits BUY/SELL on random-walk test bars. To exercise the lifecycle after the signal
    engine, the real ``build_signal`` runs and, where strategies produced setups, its result becomes a BUY with ATR
    levels (entry ``entry_atr_offset`` ATR below the price). Everything downstream is the real code."""
    from dataclasses import replace

    import app.scanner.service as svc
    real = svc.build_signal

    def wrapped(sigs, mctx, cfg, calib):
        r = real(sigs, mctx, cfg, calib)
        if not sigs:
            return r
        a = mctx.atr or mctx.price * 0.001
        e = mctx.price - entry_atr_offset * a
        return replace(r, decision="BUY", direction="BUY", grade="A", entry=e, stop_loss=e - 2 * a, tp1=e + 2 * a,
                       tp2=e + 4 * a, tp3=None, risk_reward=2.0)
    monkeypatch.setattr(svc, "build_signal", wrapped)


def opps():
    with session_scope() as db:
        return db.scalars(select(Opportunity).order_by(Opportunity.id)).all()


def test_scan_records_opportunities_with_a_full_decision_trace(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        rows = db.execute(select(Opportunity, AnalysisResult).join(AnalysisResult)).all()
        n_with_setups = sum(1 for a in db.scalars(select(AnalysisResult)) if a.score.get("strategies"))
        assert rows and len(rows) == n_with_setups, "one opportunity per analysis with at least one setup"
        for o, a in rows:
            stages = [t["stage"] for t in o.trace]
            assert stages[:3] == ["data", "market_regime", "strategies"] and stages[-1] == "decision"
            assert {"fundamental", "entry_validation", "exit_plan", "failure_prediction"} <= set(stages)
            assert o.trigger == "bar_close" and o.bar_time == a.bar_time
            sig = db.get(TradingSignal, o.signal_id) if o.signal_id else None
            d = a.score["decision"]
            expected = {"NO_TRADE": "NO_TRADE", "WAIT": "WAITING_FOR_CONFIRMATION", "BUY": "MONITORING",
                        "SELL": "MONITORING"}[d]
            if o.score_breakdown["entry_validation"] == "FAIL":
                expected = "REJECTED"
            assert o.state == expected, (o.state, d, o.state_reason)
            assert (sig is not None) == (d in ("BUY", "SELL", "WAIT"))
            hist = db.scalars(select(OpportunityTransition).where(OpportunityTransition.opportunity_id == o.id)
                              .order_by(OpportunityTransition.id)).all()
            assert [h.to_state for h in hist] == ["RESEARCHING", o.state]
            # calendar unavailable in tests: said so, never "no news"
            fund = next(t for t in o.trace if t["stage"] == "fundamental")
            assert fund["result"] == "unavailable"
            # advisory mode: the engine's decision is kept unless the stop/target geometry is wrong
            dec = o.trace[-1]
            if not dec["evidence"]["gate"]:
                assert dec["result"] == dec["evidence"]["engine_decision"] == d
        ran = {r.agent_id for r in db.scalars(select(AgentRun))}
    assert NEW_AGENTS <= ran, ran


def test_opportunity_and_source_registry_api(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    from app.main import app
    make_user("viewer27", "readonly")
    with TestClient(app) as c:
        assert c.get("/api/opportunities").status_code == 401
        login(c, "viewer27")
        lst = c.get("/api/opportunities").json()
        assert lst and all(set(x) >= {"id", "symbol", "state", "trigger", "score_breakdown"} for x in lst)
        one = c.get(f"/api/opportunities/{lst[0]['id']}").json()
        assert one["trace"] and one["transitions"][0]["to"] == "RESEARCHING"
        assert c.get("/api/opportunities/999999").status_code == 404
        assert c.get("/api/opportunities", params={"state": "BOGUS"}).status_code == 422
        summ = c.get("/api/opportunities/summary").json()
        assert sum(summ["states"].values()) == len(opps()) and summ["gate_mode"] == "advisory"
        src = {s["key"]: s for s in c.get("/api/data-sources").json()["sources"]}
        assert src["mt5_market_data"]["status"] == "ok" and src["mt5_market_data"]["ok_count"] >= 1
        assert src["economic_calendar"]["status"] == "not_configured"


def test_ai_review_moves_opportunities_and_sync_follows_the_signal(scan, monkeypatch):
    fake, bridge, _ = scan
    force_buy(monkeypatch)

    class Reviewer:
        model = "scripted"

        def throttle_ok(self, *_):
            return True

        def review(self, cand):
            return Review.model_validate({**REVIEW_OK, "decision": cand["direction"]}), \
                {"provider": "scripted", "model": "m", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1.0}
    scan_once(bridge, Reviewer(), lambda _id: None)
    with session_scope() as db:
        q = db.scalars(select(Opportunity).where(Opportunity.state == "QUALIFIED")).all()
        assert q, [(o.state, o.state_reason) for o in db.scalars(select(Opportunity))]
        o = q[0]
        assert o.trace[-1]["stage"] == "ai_review" and o.score_breakdown["ai_review"] == o.direction
        sig = db.get(TradingSignal, o.signal_id)
        sig.status = "EXECUTED"
        db.add(RiskEvaluation(signal_id=sig.id, approved=True, checks=[], reasons=["all checks passed"]))
        oid = o.id
    with session_scope() as db:
        expire_signals(db)
    with session_scope() as db:
        o = db.get(Opportunity, oid)
        assert o.state == "EXECUTED" and [t["stage"] for t in o.trace[-2:]] == ["risk_engine", "execution"]


def test_enforce_gate_only_downgrades(scan, monkeypatch):
    fake, bridge, _ = scan
    force_buy(monkeypatch, entry_atr_offset=1.0)               # the price is 1 ATR past the entry: chased
    monkeypatch.setenv("RR_OPPORTUNITY_GATE", "enforce")
    get_settings.cache_clear()
    try:
        scan_once(bridge, None, lambda _id: None)
    finally:
        get_settings.cache_clear()
    with session_scope() as db:
        sigs = db.scalars(select(TradingSignal)).all()
        assert sigs and not any(s.decision in ("BUY", "SELL") for s in sigs), "every entry is chased: no BUY/SELL"
        downgraded = [a for a in db.scalars(select(AnalysisResult))
                      if any("enforce mode" in w for w in a.score.get("warnings", []))]
        assert downgraded and all(a.score["decision"] == "WAIT" for a in downgraded)
        assert {o.state for o in db.scalars(select(Opportunity))} <= {"WAITING_FOR_CONFIRMATION", "NO_TRADE", "REJECTED"}


# ---- event-driven re-analysis -------------------------------------------------------------------------------------------
def test_request_is_throttled_per_symbol(db_clean):
    r = get_redis()
    assert triggers.request(r, 7, ["H1"], "event:x", 60) is True
    assert triggers.request(r, 7, ["H4"], "event:y", 60) is False
    assert triggers.pending(r, 7, "H1") == "event:x" and triggers.pending(r, 7, "H4") is None


def test_event_reanalysis_supersedes_the_bars_signal_instead_of_adding_one(scan, monkeypatch):
    fake, bridge, _ = scan
    force_buy(monkeypatch)
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        sig = db.scalars(select(TradingSignal).where(TradingSignal.status == "NEW")).first()
        assert sig is not None
        sid, tf, old = sig.symbol_id, sig.timeframe, sig.id
    assert scan_once(bridge, None, lambda _id: None)["analyses"] == 0, "same bars, no event: nothing due"
    triggers.request(get_redis(), sid, [tf], "event:test", 60)
    assert scan_once(bridge, None, lambda _id: None)["analyses"] == 1
    with session_scope() as db:
        assert db.get(TradingSignal, old).status == "EXPIRED"
        assert "Superseded by re-analysis (event:test)" in db.get(TradingSignal, old).explanation
        new = db.scalars(select(TradingSignal).where(TradingSignal.symbol_id == sid, TradingSignal.timeframe == tf,
                                                     TradingSignal.status == "NEW")).all()
        assert len(new) == 1 and new[0].id != old, "exactly one NEW signal for the bar"
        o = db.scalars(select(Opportunity).where(Opportunity.trigger == "event:test")).one()
        assert o.symbol_id == sid and o.timeframe == tf
    assert triggers.pending(get_redis(), sid, tf) is None, "the request is consumed"
    # An executed signal for the bar: the re-analysis records the opportunity but creates no second signal.
    with session_scope() as db:
        for s in db.scalars(select(TradingSignal).where(TradingSignal.symbol_id == sid, TradingSignal.timeframe == tf)):
            s.status = "EXECUTED"
        n = db.query(TradingSignal).count()
    get_redis().delete(f"rr:reanalyse:throttle:{sid}")
    triggers.request(get_redis(), sid, [tf], "event:again", 60)
    scan_once(bridge, None, lambda _id: None)
    with session_scope() as db:
        assert db.query(TradingSignal).count() == n
        o = db.scalars(select(Opportunity).where(Opportunity.trigger == "event:again")).one()
        assert o.state == "REJECTED" and o.signal_id is None and "already executed" in o.state_reason


def test_news_release_requests_reanalysis_of_affected_symbols_once(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    now = datetime.now(UTC)
    with session_scope() as db:
        names = [s.broker_symbol for s in db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True)))]
        db.add(EconomicEvent(provider="generic_json", provider_id="nfp-1", title="Non-Farm Payrolls", currency="USD",
                             impact="HIGH", scheduled_at=now - timedelta(minutes=5), actual="250K", forecast="180K",
                             fetched_at=now + timedelta(seconds=1)))
    with session_scope() as db:
        tfs = get_settings().timeframes
        got = triggers.check(db, bridge, get_redis(), tfs, now + timedelta(seconds=2), 300)
        again = triggers.check(db, bridge, get_redis(), tfs, now + timedelta(seconds=3), 300)
    usd = [n for n in names if "USD" in n]
    assert {g["symbol"] for g in got} == set(usd) and all(g["reason"] == "event:news_release" for g in got)
    assert again == [], "each release fires once"


# ---- providers: OpenAI preset, parameter fallback, consensus -------------------------------------------------------------
def test_openai_preset_and_reasoning_model_parameter_fallback():
    p = next(x for x in PRESETS if x["id"] == "openai")
    assert p["base_url"] == "https://api.openai.com/v1" and p["key_required"] and "ChatGPT subscription" in p["note"]
    bodies = []

    def handler(req):
        import json
        body = json.loads(req.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                                                  "with this model. Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": '{"a": 1}'},
                                                                     "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    prov = OpenAICompatibleProvider("http://fake.local/v1", "sk-x", 5, transport=httpx.MockTransport(handler))
    out = prov.complete_json("sys", {"x": 1}, {"type": "object"}, "m", 1000)
    assert out.data == {"a": 1} and len(bodies) == 2
    assert "temperature" not in bodies[1] and bodies[1]["max_completion_tokens"] == 1000


CAND = {"symbol": "EURUSD", "timeframe": "H1", "direction": "BUY", "decision": "BUY"}


def consensus_route(script):
    a, b = provider("claude-x", kind="anthropic"), provider("openai-x")
    with session_scope() as db:
        from app.db.models_ai import AITaskRoute
        db.merge(AITaskRoute(task="candidate_review", chain=[{"provider_id": a, "model": "c1"},
                                                              {"provider_id": b, "model": "o1"}], mode="consensus"))
    return gateway.GatewayReviewer(Scripted(script).factory)


@pytest.mark.parametrize("da,db_,want", [("BUY", "BUY", "BUY"), ("BUY", "NO_TRADE", "NO_TRADE"),
                                         ("BUY", "WAIT_FOR_CONFIRMATION", "WAIT_FOR_CONFIRMATION"),
                                         ("BUY", "SELL", "NO_TRADE")])  # a reversal is NO_TRADE by the boundaries
def test_consensus_confirms_only_when_both_providers_confirm(keys, da, db_, want):
    rv = consensus_route({"claude-x": [{**REVIEW_OK, "decision": da}], "openai-x": [{**REVIEW_OK, "decision": db_}]})
    out, meta = rv.review(dict(CAND))
    assert out.decision == want and meta["mode"] == "consensus"
    assert [v["provider"] for v in meta["consensus"]["votes"]] == ["claude-x", "openai-x"]
    assert out.explanation.startswith("[AI consensus:")


def test_consensus_without_both_answers_leaves_the_candidate_unreviewed(keys):
    rv = consensus_route({"claude-x": [{**REVIEW_OK}], "openai-x": [{"decision": "BUY"}]})   # invalid schema
    with pytest.raises(AgentError, match="consensus: no valid answer from openai-x"):
        rv.review(dict(CAND))


def test_consensus_route_validation(keys, client):
    a, b = provider("p-a"), provider("p-b")
    from tests.test_api import h
    login(client, "admin")
    put = lambda task, chain, mode: client.put(f"/api/ai/routes/{task}", headers=h(client),  # noqa: E731
                                               json={"chain": chain, "mode": mode})
    one = [{"provider_id": a, "model": "m"}]
    two = one + [{"provider_id": b, "model": "m"}]
    assert put("candidate_review", one, "consensus").status_code == 422
    assert put("trade_research", two, "consensus").status_code == 422
    assert put("candidate_review", two, "consensus").json()["mode"] == "consensus"
    rows = {r["task"]: r for r in client.get("/api/ai/routes").json()}
    assert rows["candidate_review"]["mode"] == "consensus" and rows["candidate_review"]["consensus_supported"]
    assert gateway.route_mode("candidate_review") == "consensus" and gateway.route_mode("trade_research") == "fallback"
    assert put("candidate_review", two, "fallback").json()["mode"] == "fallback"
