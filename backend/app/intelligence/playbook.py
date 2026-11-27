"""Market playbooks: the AI thinks like a professional trader once per symbol and market regime; the engine saves the
plan and applies it to every candidate itself (no AI call per trade).

1. ``get_or_create`` for a candidate's symbol and current regime:
   a. a valid ACTIVE playbook for that symbol and regime -> used (no AI call);
   b. otherwise an earlier playbook for the same symbol and regime whose graded trades were profitable (at least
      ``playbook_reuse_min_trades`` trades, net > 0) -> reused for a new validity window (no AI call);
   c. otherwise, if playbooks are on, an AI provider is configured, the budget allows and the daily cap is not
      reached -> one AI call with a compact summary of the engine's own facts (multi-timeframe analysis, regime,
      volatility, spread, sessions, news, recent signals and this symbol's trade results); the plan is saved;
   d. otherwise no playbook: the engine decides exactly as without playbooks.
   A playbook for another regime is SUPERSEDED when the regime changes.
2. ``apply`` to a candidate. A playbook can only RESTRICT: stand aside, skip a direction against its plan, or reduce
   size (multiplier <= 1). It never creates or forces a trade, never raises size and never bypasses the Risk Engine.
3. ``grade`` (learner job): the trades each playbook governed (closed positions -> order -> signal -> playbook id) are
   scored; good playbooks are reused, failed ones are not.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models_ai import AIControl, MarketPlaybook

UTC = timezone.utc
TIMEFRAMES = ("M15", "H1", "H4", "D1")

SYSTEM = (
    "You are a professional discretionary trader writing a trading plan for one instrument, to be applied "
    "automatically by a rule-based engine for the next hours. Use ONLY the facts given (the engine's own multi-timeframe "
    "analysis, regime, volatility, spread, sessions, news and its recent results on this instrument); do not invent "
    "prices, levels or statistics. Decide the directional bias, which directions are allowed, which of the listed "
    "strategy families suit this market, conditions to avoid, and the risk posture: 'normal', 'reduced' (choppy, "
    "conflicting timeframes, news nearby, poor recent results) or 'stand_aside' (no edge). Prefer standing aside over "
    "forcing trades. You cannot raise risk: size_multiplier is at most 1. Keep the reasoning short and specific.")

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["bias", "allowed_directions", "preferred_strategies", "avoid", "risk_posture", "size_multiplier",
                 "reasoning"],
    "properties": {
        "bias": {"type": "string", "enum": ["BUY", "SELL", "NEUTRAL"]},
        "allowed_directions": {"type": "array", "items": {"type": "string", "enum": ["BUY", "SELL"]}, "maxItems": 2},
        "preferred_strategies": {"type": "array", "items": {"type": "string", "maxLength": 96}, "maxItems": 12},
        "avoid": {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 8},
        "risk_posture": {"type": "string", "enum": ["normal", "reduced", "stand_aside"]},
        "size_multiplier": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "maxLength": 1500},
    },
}


class Plan(BaseModel):
    bias: str
    allowed_directions: list[str]
    preferred_strategies: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    risk_posture: str
    size_multiplier: float
    reasoning: str


def settings(db: Session) -> dict:
    c = db.get(AIControl, 1)
    return {"enabled": True if c is None else bool(c.playbook_enabled),
            "hours": int(c.playbook_hours) if c and c.playbook_hours else 12,
            "daily_cap": int(c.playbook_daily_cap) if c and c.playbook_daily_cap is not None else 12,
            "reuse_min_trades": int(c.playbook_reuse_min_trades) if c and c.playbook_reuse_min_trades else 3}


def facts_for(db: Session, sym, regime: str) -> dict:
    """A compact summary of the engine's own facts for one symbol (never raw candles or ticks)."""
    from app.db.models import AnalysisResult, ClosedPosition, TradingSignal
    tfs = {}
    for tf in TIMEFRAMES:
        ar = db.scalar(select(AnalysisResult).where(AnalysisResult.symbol_id == sym.id, AnalysisResult.timeframe == tf)
                       .order_by(AnalysisResult.id.desc()).limit(1))
        if ar is None:
            continue
        st = ar.structure or {}
        tfs[tf] = {"regime": ar.regime, "regime_detail": st.get("regime"), "htf_bias": st.get("htf_bias"),
                   "atr": st.get("atr"), "price": st.get("price"), "spread_points": st.get("spread_points"),
                   "sessions": st.get("sessions"), "news": st.get("news"),
                   "signals": [{"strategy": o.get("strategy"), "direction": o.get("direction")}
                               for o in (ar.strategy_outputs or [])[:12] if isinstance(o, dict) and o.get("strategy")],
                   "at": ar.bar_time.isoformat() if ar.bar_time else None}
    since = datetime.now(UTC) - timedelta(days=30)
    sigs = db.scalars(select(TradingSignal).where(TradingSignal.symbol_id == sym.id, TradingSignal.created_at >= since)
                      .order_by(TradingSignal.id.desc()).limit(15)).all()
    closed = db.scalars(select(ClosedPosition).where(ClosedPosition.symbol == sym.broker_symbol,
                                                     ClosedPosition.closed_at >= since)
                        .order_by(ClosedPosition.closed_at.desc()).limit(20)).all()
    strategies = sorted({o["strategy"] for t in tfs.values() for o in t["signals"]})
    return json.loads(json.dumps({
        "symbol": sym.broker_symbol, "category": sym.category, "current_regime": regime, "timeframes": tfs,
        "strategy_families_available": strategies,
        "recent_signals": [{"decision": s.decision, "grade": s.grade, "timeframe": s.timeframe, "score": s.score,
                            "regime": s.regime, "status": s.status} for s in sigs],
        "recent_results": [{"direction": c.direction, "profit": c.profit, "r": c.r_multiple, "regime": c.regime,
                            "strategies": c.strategy_codes, "close_reason": c.close_reason} for c in closed],
    }, default=str))


def _active(db: Session, symbol: str, now: datetime) -> list[MarketPlaybook]:
    return db.scalars(select(MarketPlaybook).where(MarketPlaybook.symbol == symbol, MarketPlaybook.status == "ACTIVE")
                      .order_by(MarketPlaybook.id.desc())).all()


def get_or_create(db: Session, sym, regime: str, provider_factory=None) -> tuple[MarketPlaybook | None, str]:
    """(playbook or None, how it was obtained: active | reused | ai | none:<why>)."""
    now = datetime.now(UTC)
    cfg = settings(db)
    if not cfg["enabled"]:
        return None, "none:playbooks off"
    for pb in _active(db, sym.broker_symbol, now):
        if pb.valid_until <= now:
            pb.status = "EXPIRED"
        elif pb.regime != regime:
            pb.status = "SUPERSEDED"                          # the market changed: the old plan no longer applies
        else:
            return pb, "active"
    until = now + timedelta(hours=cfg["hours"])
    good = db.scalars(select(MarketPlaybook).where(MarketPlaybook.symbol == sym.broker_symbol,
                                                   MarketPlaybook.regime == regime,
                                                   MarketPlaybook.status.in_(["EXPIRED", "SUPERSEDED"]))
                      .order_by(MarketPlaybook.id.desc()).limit(20)).all()
    for old in good:
        o = old.outcomes or {}
        if int(o.get("trades") or 0) >= cfg["reuse_min_trades"] and float(o.get("net") or 0) > 0:
            pb = MarketPlaybook(symbol=old.symbol, regime=old.regime, bias=old.bias,
                                allowed_directions=list(old.allowed_directions or []),
                                preferred_strategies=list(old.preferred_strategies or []), avoid=list(old.avoid or []),
                                risk_posture=old.risk_posture, size_multiplier=old.size_multiplier,
                                reasoning=old.reasoning, facts=old.facts, source="reused",
                                reused_from=old.reused_from or old.id, ai_provider=old.ai_provider, tokens=0,
                                status="ACTIVE", outcomes={}, valid_from=now, valid_until=until)
            db.add(pb)
            db.flush()
            return pb, "reused"
    from app.ai import gateway
    if not gateway.configured("market_playbook"):
        return None, "none:no AI provider"
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    made = db.scalar(select(func.count(MarketPlaybook.id)).where(MarketPlaybook.source == "ai",
                                                                MarketPlaybook.created_at >= day)) or 0
    if made >= cfg["daily_cap"]:
        return None, f"none:daily cap {cfg['daily_cap']} reached"
    facts = facts_for(db, sym, regime)
    try:
        plan, meta = gateway.run("market_playbook", SYSTEM, facts, SCHEMA, Plan, max_tokens=2000,
                                 provider_factory=provider_factory)
    except gateway.GatewayError as exc:
        return None, f"none:AI unavailable ({str(exc)[:120]})"
    allowed = sorted({d for d in plan.allowed_directions if d in ("BUY", "SELL")})
    if plan.risk_posture == "stand_aside":
        allowed = []
    pb = MarketPlaybook(symbol=sym.broker_symbol, regime=regime, bias=plan.bias, allowed_directions=allowed,
                        preferred_strategies=plan.preferred_strategies[:12], avoid=plan.avoid[:8],
                        risk_posture=plan.risk_posture, size_multiplier=max(0.0, min(1.0, float(plan.size_multiplier))),
                        reasoning=plan.reasoning, facts=facts, source="ai",
                        ai_provider=f"{meta['provider']}/{meta['model']}"[:160],
                        tokens=int(meta.get("input_tokens") or 0) + int(meta.get("output_tokens") or 0),
                        status="ACTIVE", outcomes={}, valid_from=now, valid_until=until)
    db.add(pb)
    db.flush()
    return pb, "ai"


def apply(pb: MarketPlaybook, direction: str) -> dict:
    """The playbook's verdict for one candidate: it can only restrict (stand aside, skip a direction, reduce size)."""
    base = {"id": pb.id, "bias": pb.bias, "posture": pb.risk_posture, "source": pb.source}
    if pb.risk_posture == "stand_aside" or not pb.allowed_directions:
        return {**base, "allow": False, "reason": f"playbook #{pb.id}: stand aside on {pb.symbol} ({pb.regime})"}
    if direction not in pb.allowed_directions:
        return {**base, "allow": False, "reason": f"playbook #{pb.id}: {direction} is against the plan "
                                                  f"({', '.join(pb.allowed_directions)} only)"}
    mult = max(0.0, min(1.0, float(pb.size_multiplier or 1.0)))
    if pb.risk_posture == "reduced":
        mult = min(mult, 0.5)
    return {**base, "allow": True, "size_multiplier": round(mult, 3),
            "reason": f"playbook #{pb.id}: {direction} fits the plan ({pb.risk_posture})"}


def grade(db: Session) -> dict:
    """Score every playbook from the trades it governed (closed positions -> order -> signal -> playbook)."""
    from app.db.models import ClosedPosition, TradeOrder, TradingSignal
    rows = db.execute(select(TradingSignal.ai_review, ClosedPosition.profit, ClosedPosition.commission,
                             ClosedPosition.swap, ClosedPosition.r_multiple)
                      .join(TradeOrder, TradeOrder.signal_id == TradingSignal.id)
                      .join(ClosedPosition, ClosedPosition.execution_id == TradeOrder.execution_id)).all()
    agg: dict[int, dict] = {}
    for review, p, c, s, r in rows:
        pid = ((review or {}).get("playbook") or {}).get("id")
        if not pid:
            continue
        a = agg.setdefault(int(pid), {"trades": 0, "wins": 0, "net": 0.0, "r": []})
        net = float(p or 0) + float(c or 0) + float(s or 0)
        a["trades"] += 1
        a["wins"] += int(net > 0)
        a["net"] = round(a["net"] + net, 2)
        if r is not None:
            a["r"].append(float(r))
    for pid, a in agg.items():
        pb = db.get(MarketPlaybook, pid)
        if pb is not None:
            pb.outcomes = {"trades": a["trades"], "wins": a["wins"], "net": a["net"],
                           "avg_r": round(sum(a["r"]) / len(a["r"]), 3) if a["r"] else None,
                           "graded_at": datetime.now(UTC).isoformat()}
    now = datetime.now(UTC)
    expired = 0
    for pb in db.scalars(select(MarketPlaybook).where(MarketPlaybook.status == "ACTIVE",
                                                      MarketPlaybook.valid_until <= now)):
        pb.status = "EXPIRED"
        expired += 1
    return {"graded": len(agg), "expired": expired}
