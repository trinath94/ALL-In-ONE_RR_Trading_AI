"""Opportunity lifecycle, decision trace and the Part 27 deterministic specialist views (Parts 27-30 Phase 4).

Every analysis in which at least one strategy produced a setup becomes an *opportunity*. Its state follows the
lifecycle below, and its decision trace records, stage by stage, what each agent actually saw and decided:

    data -> market_regime -> strategies -> news / fundamental -> signal engine -> adaptive -> consensus / probability
    -> fusion -> entry validation -> exit plan -> failure prediction -> AI review -> Risk Engine -> execution

The four new specialist views are deterministic and computed from data the engine already has. They never size a
position, submit an order or loosen anything:

* **entry validation**: geometry of the levels (always enforced: a stop or target on the wrong side of the entry is
  a defect, so the setup becomes NO_TRADE) and price/spread checks (advisory unless RR_OPPORTUNITY_GATE=enforce, which
  downgrades BUY/SELL to WAIT);
* **exit plan**: the targets in R multiples and the Risk Engine's own break-even / partial / trailing parameters;
* **failure prediction**: a count of named risk factors with their evidence. It is *not* a probability; with
  RR_OPPORTUNITY_GATE=enforce a HIGH level downgrades BUY/SELL to WAIT;
* **fundamental view**: the economic calendar for the symbol's currencies (upcoming high-impact events, recent
  releases with actual vs forecast). Without a healthy calendar provider it says "unavailable", never "no news".
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_opportunity import Opportunity, OpportunityTransition

STATES = ("RESEARCHING", "MONITORING", "WAITING_FOR_CONFIRMATION", "QUALIFIED", "REJECTED", "NO_TRADE", "EXPIRED",
          "EXECUTED")
TERMINAL = frozenset({"REJECTED", "NO_TRADE", "EXPIRED", "EXECUTED"})
_LIVE = ("MONITORING", "WAITING_FOR_CONFIRMATION", "QUALIFIED")
ALLOWED: dict[str | None, frozenset[str]] = {
    None: frozenset({"RESEARCHING"}),
    "RESEARCHING": frozenset({"MONITORING", "WAITING_FOR_CONFIRMATION", "REJECTED", "NO_TRADE", "EXPIRED"}),
    "MONITORING": frozenset({"WAITING_FOR_CONFIRMATION", "QUALIFIED", "REJECTED", "NO_TRADE", "EXPIRED", "EXECUTED"}),
    "WAITING_FOR_CONFIRMATION": frozenset({"MONITORING", "QUALIFIED", "REJECTED", "NO_TRADE", "EXPIRED", "EXECUTED"}),
    "QUALIFIED": frozenset({"WAITING_FOR_CONFIRMATION", "REJECTED", "EXPIRED", "EXECUTED"}),
    **{t: frozenset() for t in TERMINAL},
}
MIN_TRADES = 30          # below this, historical evidence is inconclusive (same threshold the AI prompts use)


class TransitionError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def stage(name: str, agent: str, result, evidence: dict | None = None) -> dict:
    return {"stage": name, "agent": agent, "at": now().isoformat(), "result": result, "evidence": evidence or {}}


# ---- deterministic specialist views ---------------------------------------------------------------------------------
def entry_validation(res, price: float | None, spread: float | None, atr: float | None, *, max_chase_atr: float,
                     max_spread_sl: float) -> dict:
    """Checks on a BUY/SELL/WAIT setup's levels against the current quote. outcome: PASS | WAIT | FAIL | N/A."""
    d = res.direction
    if res.decision not in ("BUY", "SELL", "WAIT") or d not in ("BUY", "SELL"):
        return {"outcome": "N/A", "checks": [], "reason": f"engine decision {res.decision}"}
    e, sl, tp = res.entry, res.stop_loss, res.tp1
    checks: list[dict] = []

    def add(name, ok, hard, value=None, limit=None, note=""):
        checks.append({"check": name, "ok": bool(ok), "hard": hard, "value": value, "limit": limit, "note": note})

    if e is None or sl is None:
        if res.decision == "WAIT":                 # the engine's WAIT need not carry levels yet: nothing to validate
            return {"outcome": "N/A", "checks": [], "reason": "WAIT without entry levels"}
        add("levels_present", False, True, note="entry or stop loss missing")
        return {"outcome": "FAIL", "checks": checks, "reason": "entry or stop loss missing"}
    sgn = 1 if d == "BUY" else -1
    risk = abs(e - sl)
    add("stop_on_correct_side", (e - sl) * sgn > 0, True, round(sl, 8), note=f"{d}: stop must be {'below' if sgn > 0 else 'above'} entry")
    if tp is not None:
        add("target_on_correct_side", (tp - e) * sgn > 0, True, round(tp, 8))
    if price is not None and price > 0:
        add("price_not_beyond_stop", (price - sl) * sgn > 0, False, round(price, 8), round(sl, 8),
            "the current price has already reached the stop" if (price - sl) * sgn <= 0 else "")
        if atr:
            chase = abs(price - e) / atr
            add("entry_not_chased", chase <= max_chase_atr, False, round(chase, 3), max_chase_atr,
                "distance from entry to the current price, in ATR")
    else:
        add("current_price_known", False, False, note="no current quote")
    if spread is not None and risk > 0:
        ratio = spread / risk
        add("spread_vs_stop", ratio <= max_spread_sl, False, round(ratio, 4), max_spread_sl,
            "spread as a fraction of the stop distance")
    if any(not c["ok"] and c["hard"] for c in checks):
        outcome = "FAIL"
    elif any(not c["ok"] for c in checks):
        outcome = "WAIT"
    else:
        outcome = "PASS"
    failed = [c["check"] for c in checks if not c["ok"]]
    return {"outcome": outcome, "checks": checks, "reason": ", ".join(failed) or "all checks passed"}


def exit_plan(res, params) -> dict:
    """Targets in R multiples and the Risk Engine's management parameters (read, never changed)."""
    if res.direction not in ("BUY", "SELL") or res.entry is None or res.stop_loss is None:
        return {"available": False, "reason": "no directional setup with levels"}
    risk = abs(res.entry - res.stop_loss)
    if risk <= 0:
        return {"available": False, "reason": "zero stop distance"}
    targets = []
    for name in ("tp1", "tp2", "tp3"):
        v = getattr(res, name)
        if v is not None:
            targets.append({"target": name, "price": v, "r_multiple": round(abs(v - res.entry) / risk, 2)})
    mgmt = None
    if params is not None:
        mgmt = {k: getattr(params, k) for k in ("break_even_at_r", "partial_close_at_r", "partial_close_fraction",
                                                "trailing_start_r", "trailing_atr_mult") if hasattr(params, k)}
    return {"available": True, "risk_distance": risk, "targets": targets,
            "order_take_profit": "tp2" if res.tp2 is not None else None,     # what the trade execution agent sends
            "management": mgmt, "management_source": "Risk Engine parameters for the active mode" if mgmt else None,
            "invalidation": list(res.invalidation or []), "expires_at": res.expires_at.isoformat()}


def failure_prediction(res, stats: dict, intel: dict, news: str, regime_tags: list[str], htf_bias: int) -> dict:
    """Named risk factors with their evidence. level: LOW | MEDIUM | HIGH | N/A. Not a probability."""
    if res.direction not in ("BUY", "SELL"):
        return {"level": "N/A", "factors": [], "reason": f"engine decision {res.decision}"}
    factors: list[dict] = []
    codes = sorted({x["strategy"] for x in res.strategies if x.get("direction") in (None, res.direction)})
    trades = sum((stats.get(c) or {}).get("trades", 0) for c in codes)
    wins = sum((stats.get(c) or {}).get("wins", 0) for c in codes)
    history = {"strategies": codes, "trades": trades, "win_rate": round(wins / trades, 4) if trades else None,
               "sources": "OOS, walk-forward, paper and live only"}
    sufficient = trades >= MIN_TRADES
    if not sufficient:
        factors.append({"factor": "insufficient_history", "evidence": f"{trades} out-of-sample trades (< {MIN_TRADES})"})
    elif wins / trades < 0.40:
        factors.append({"factor": "weak_history", "evidence": f"win rate {wins / trades:.0%} over {trades} trades"})
    found = (intel.get("anomalies") or {}).get("found") or []
    for a in found[:5]:
        factors.append({"factor": f"anomaly_{a.get('kind', 'unknown')}", "evidence": f"{a.get('severity', '')}: {a.get('explanation', '')}".strip(": ")})
    if news == "RESTRICTED":
        factors.append({"factor": "news_window", "evidence": "high-impact event inside the restriction window"})
    elif news == "UNKNOWN":
        factors.append({"factor": "news_unknown", "evidence": "economic calendar unavailable or stale"})
    for t in ("HIGH_VOLATILITY", "VOLATILITY_EXPANSION", "UNCERTAIN"):
        if t in regime_tags:
            factors.append({"factor": f"regime_{t.lower()}", "evidence": f"regime tag {t}"})
    sgn = 1 if res.direction == "BUY" else -1
    if htf_bias and htf_bias * sgn < 0:
        factors.append({"factor": "against_higher_timeframe", "evidence": f"higher-timeframe bias {htf_bias}"})
    prob = intel.get("probability") or {}
    p = prob.get("tp_before_sl_probability") if isinstance(prob, dict) else None
    if prob.get("available") and isinstance(p, (int, float)) and p < 0.45:
        factors.append({"factor": "low_model_probability", "evidence": f"P(TP before SL) {p:.2f}"})
    cons = intel.get("consensus") or {}
    if cons.get("decision") and cons["decision"] != res.direction:
        factors.append({"factor": "consensus_disagrees", "evidence": f"consensus {cons['decision']}"})
    blocking = (intel.get("anomalies") or {}).get("blocking") or []
    n = len([f for f in factors if f["factor"] != "insufficient_history"])
    level = "HIGH" if blocking or n >= 3 else ("MEDIUM" if n >= 1 or not sufficient else "LOW")
    return {"level": level, "factors": factors, "history": history, "evidence_sufficient": sufficient,
            "method": "rule-based count of named risk factors (not a probability)"}


_NUM = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([%KMBT]?)\s*$", re.IGNORECASE)
_MULT = {"": 1, "%": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _num(v: str | None) -> float | None:
    m = _NUM.match(v or "")
    return float(m.group(1)) * _MULT[m.group(2).upper()] if m else None


def fundamental_view(db: Session, sym, name: str, at: datetime | None = None) -> dict:
    """Economic calendar context for the symbol's currencies. No calendar: available False (never 'no news')."""
    from app.db.models import EconomicEvent
    from app.news.service import is_high_impact, provider_healthy, symbol_currencies
    if not provider_healthy():
        return {"available": False, "reason": "economic calendar not configured or stale"}
    at = at or now()
    ccys = symbol_currencies(sym, name)
    evs = db.scalars(select(EconomicEvent).where(EconomicEvent.currency.in_(ccys),
                                                 EconomicEvent.scheduled_at.between(at - timedelta(hours=6),
                                                                                    at + timedelta(hours=24)))
                     .order_by(EconomicEvent.scheduled_at)).all()
    upcoming = [e for e in evs if e.scheduled_at > at and is_high_impact(e)]
    released = []
    for e in evs:
        if e.scheduled_at <= at and e.actual is not None:
            a, f = _num(e.actual), _num(e.forecast)
            released.append({"title": e.title, "currency": e.currency, "impact": e.impact,
                             "at": e.scheduled_at.isoformat(), "actual": e.actual, "forecast": e.forecast,
                             "surprise": None if a is None or f is None else ("above" if a > f else "below" if a < f else "inline")})
    recent_high = [r for r in released if r["impact"] == "HIGH" and
                   datetime.fromisoformat(r["at"]) >= at - timedelta(hours=1)]
    return {"available": True, "currencies": ccys,
            "upcoming_high_impact": [{"title": e.title, "currency": e.currency, "at": e.scheduled_at.isoformat()}
                                     for e in upcoming[:5]],
            "next_high_impact_minutes": round((upcoming[0].scheduled_at - at).total_seconds() / 60) if upcoming else None,
            "recent_releases": released[-5:], "high_impact_released_last_hour": len(recent_high),
            "note": "surprise compares actual with forecast only; its market effect is not inferred"}


# ---- lifecycle -------------------------------------------------------------------------------------------------------
def transition(db: Session, opp: Opportunity, to: str, reason: str) -> bool:
    """Move an opportunity to ``to`` if the lifecycle allows it; record the transition. Same state: no-op."""
    if to not in STATES:
        raise TransitionError(f"unknown state {to}")
    if opp.state == to:
        return False
    if to not in ALLOWED.get(opp.state, frozenset()):
        raise TransitionError(f"{opp.state} -> {to} is not allowed")
    db.add(OpportunityTransition(opportunity_id=opp.id, from_state=opp.state, to_state=to, reason=reason[:2000], at=now()))
    opp.state, opp.state_reason, opp.updated_at = to, reason[:2000], now()
    return True


def initial_state(res, entry: dict) -> tuple[str, str]:
    if entry.get("outcome") == "FAIL":                      # checked first: the gate has already made it NO_TRADE
        return "REJECTED", f"entry validation failed: {entry.get('reason')}"
    if res.decision == "NO_TRADE":
        return "NO_TRADE", "signal engine: no trade"
    if res.decision == "WAIT":
        return "WAITING_FOR_CONFIRMATION", "signal engine: wait for confirmation"
    return "MONITORING", f"signal engine {res.decision} {res.grade} ({res.score}); awaiting shortlist and AI review"


def create(db: Session, *, symbol_id: int, timeframe: str, bar_time: datetime, analysis_id: int, res, trigger: str,
           trace: list[dict], breakdown: dict, entry: dict) -> Opportunity:
    opp = Opportunity(symbol_id=symbol_id, timeframe=timeframe, bar_time=bar_time, analysis_id=analysis_id,
                      trigger=trigger[:64], direction=res.direction, state="RESEARCHING", state_reason="analysis started",
                      score_breakdown=breakdown, trace=trace, created_at=now(), updated_at=now())
    db.add(opp)
    db.flush()
    db.add(OpportunityTransition(opportunity_id=opp.id, from_state=None, to_state="RESEARCHING",
                                 reason=f"analysis ({trigger})", at=now()))
    state, why = initial_state(res, entry)
    transition(db, opp, state, why)
    return opp


def append_trace(opp: Opportunity, entry: dict) -> None:
    opp.trace = [*(opp.trace or []), entry]           # new list: JSONB change is detected


def after_review(db: Session, opp: Opportunity, review: dict, meta: dict | None) -> None:
    """The AI review stage. Confirmed -> QUALIFIED; NO_TRADE -> REJECTED; wait -> WAITING; unreviewed: unchanged."""
    d = review.get("decision")
    append_trace(opp, stage("ai_review", "signal_quality", d, {
        "provider": (meta or {}).get("provider"), "model": (meta or {}).get("model"), "mode": (meta or {}).get("mode"),
        "consensus": (meta or {}).get("consensus"), "error": review.get("error"),
        "boundary": "AI may only confirm, downgrade to WAIT or reject"}))
    opp.score_breakdown = {**(opp.score_breakdown or {}), "ai_review": d}
    if opp.state in TERMINAL:
        return
    if d in ("BUY", "SELL") and d == opp.direction:
        transition(db, opp, "QUALIFIED", f"AI review confirmed {d}; Risk Engine decides at execution")
    elif d == "NO_TRADE":
        transition(db, opp, "REJECTED", "AI review rejected the setup")
    elif d == "WAIT_FOR_CONFIRMATION":
        transition(db, opp, "WAITING_FOR_CONFIRMATION", "AI review: wait for confirmation")
    elif d == "SKIPPED" and review.get("mode") == "ENGINE_ONLY" and opp.direction in ("BUY", "SELL"):
        transition(db, opp, "QUALIFIED", "engine-only mode (AI review off); Risk Engine decides at execution")


def sync(db: Session, limit: int = 500) -> int:
    """Follow the linked signals: EXECUTED, rejected by the Risk Engine, or expired. Records the Risk Engine stage."""
    from app.db.models import TradingSignal
    n = 0
    rows = db.execute(select(Opportunity, TradingSignal).join(TradingSignal, TradingSignal.id == Opportunity.signal_id)
                      .where(Opportunity.state.in_(_LIVE), TradingSignal.status != "NEW").limit(limit)).all()
    for opp, sig in rows:
        ev = _risk_evaluation(db, sig.id)
        if ev is not None:
            append_trace(opp, stage("risk_engine", "portfolio_risk", "APPROVED" if ev.approved else "REJECTED",
                                    {"reasons": ev.reasons, "risk_evaluation_id": ev.id}))
        if sig.status == "EXECUTED":
            append_trace(opp, stage("execution", "trade_execution", "EXECUTED", {"signal_id": sig.id}))
            transition(db, opp, "EXECUTED", "order confirmed by the broker")
        elif sig.status == "REJECTED":
            transition(db, opp, "REJECTED", "Risk Engine rejected the order" if ev is not None and not ev.approved
                       else "signal rejected")
        elif sig.status == "EXPIRED":
            transition(db, opp, "EXPIRED", "signal expired before execution")
        elif sig.status == "CANCELLED":
            transition(db, opp, "REJECTED", "signal cancelled")
        else:
            continue
        n += 1
    return n


def _risk_evaluation(db: Session, signal_id: int):
    from app.db.models import RiskEvaluation
    return db.scalar(select(RiskEvaluation).where(RiskEvaluation.signal_id == signal_id)
                     .order_by(RiskEvaluation.id.desc()).limit(1))


# ---- Master upgrade 7.2: the structured pre-trade analysis ----------------------------------------------------------
def thesis(opp: Opportunity) -> dict:
    """The trade preparation, from the opportunity's own decision trace (recorded at analysis time, before any order):
    why the opportunity exists, what supports and contradicts it, what invalidates it, why the entry, stop and target,
    what could make it fail, and whether it should be rejected. Deterministic; nothing is invented: a stage that was
    not recorded is reported as unavailable."""
    st = {t.get("stage"): t for t in (opp.trace or [])}
    ev = lambda name: (st.get(name) or {}).get("evidence") or {}            # noqa: E731
    res = lambda name: (st.get(name) or {}).get("result")                   # noqa: E731
    direction = opp.direction
    top = ev("strategies").get("top") or []
    support = [x for x in top if x.get("direction") == direction]
    against = [x for x in top if direction and x.get("direction") and x.get("direction") != direction]
    eng = ev("signal_engine")
    cons = ev("consensus")
    align = ev("timeframe_alignment")
    entry = ev("entry_validation")
    plan = ev("exit_plan")
    fail = ev("failure_prediction")
    patt = ev("patterns")
    favourable, contradicting = [], []
    if res("market_regime"):
        favourable.append(f"regime {res('market_regime')} ({', '.join(ev('market_regime').get('tags') or []) or 'no tags'})")
    if align.get("aligned") is True:
        favourable.append("higher timeframes aligned")
    elif align.get("aligned") is False:
        contradicting.append("higher timeframes not aligned")
    if res("consensus") and res("consensus") == direction:
        favourable.append(f"strategy consensus {res('consensus')} (agreement {cons.get('agreement')})")
    elif res("consensus"):
        contradicting.append(f"strategy consensus {res('consensus')}")
    for c in cons.get("conflicts") or []:
        contradicting.append(f"conflict: {c}")
    if eng.get("opposing_score"):
        contradicting.append(f"opposing score {eng.get('opposing_score')}")
    if against:
        contradicting.append("opposing strategies: " + ", ".join(x["strategy"] for x in against[:5]))
    rel = patt.get("relation_to_engine")
    if isinstance(rel, dict):
        if rel.get("agree"):
            favourable.append("patterns agree: " + ", ".join(rel["agree"][:5]))
        if rel.get("disagree"):
            contradicting.append("patterns disagree: " + ", ".join(rel["disagree"][:5]))
    if res("news") in ("RESTRICTED", "UNKNOWN"):
        contradicting.append(f"news status {res('news')}")
    if res("anomalies"):
        contradicting.append(f"{res('anomalies')} market anomalies")
    failed_checks = [c for c in entry.get("checks") or [] if not c.get("ok")]
    reject = []
    if direction not in ("BUY", "SELL"):
        reject.append(f"no directional setup (decision {res('decision')})")
    if entry.get("outcome") == "FAIL":
        reject.append("entry validation failed: " + ", ".join(c["check"] for c in failed_checks if c.get("hard")))
    if fail.get("level") == "HIGH":
        reject.append("high failure risk")
    if opp.state == "REJECTED":
        reject.append(opp.state_reason or "rejected")
    return {
        "opportunity_id": opp.id, "direction": direction, "state": opp.state,
        "why": (f"{len(support)} strategies signal {direction}; engine {res('signal_engine')}, score {eng.get('score')}"
                if direction in ("BUY", "SELL") else f"no trade setup: {res('decision')}"),
        "supporting_strategies": support,
        "supporting_patterns": rel if rel is not None else "unavailable",
        "favourable_conditions": favourable,
        "contradicting_conditions": contradicting,
        "invalidation": plan.get("invalidation") or [],
        "entry_justification": {"outcome": entry.get("outcome", "unavailable"), "checks": entry.get("checks") or [],
                                "reason": entry.get("reason")},
        "stop_justification": ({"risk_distance": plan.get("risk_distance"),
                                "management": plan.get("management")} if plan.get("available") else "unavailable"),
        "target_justification": plan.get("targets") if plan.get("available") else "unavailable",
        "failure_risks": {"level": fail.get("level", "unavailable"), "factors": fail.get("factors") or []},
        "should_reject": bool(reject), "reject_reasons": reject,
        "ai_review": res("ai_review"), "risk_engine": res("risk_engine"),
    }
