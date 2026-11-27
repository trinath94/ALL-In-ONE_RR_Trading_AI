"""A+ setups only: the checklist a professional trader runs before taking a trade.

Seven checks, one point each, from the engine's own records (no AI call):
1. grade A+ (the engine's top grade);
2. the higher timeframe agrees with the direction;
3. the entry is at a key level (within ``key_level_max_atr`` ATR, or 1 ATR when that is off);
4. at least two strategies agree;
5. the market playbook's bias agrees (an existing plan only: this never asks the AI);
6. calibrated confidence of at least 55 %;
7. reward:risk of at least 2.

The gate (``min_setup_score`` > 0, or ``key_level_max_atr`` > 0) runs in the scanner before any AI call, so a weak setup
costs no tokens. It can only hold a trade back; it never creates or forces one, and the Risk Engine still decides.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_SCORE = 7
DEFAULT_LEVEL_ATR = 1.0
MIN_CONFIDENCE = 0.55
MIN_RR = 2.0


def assess(db: Session, sig, ar, params) -> dict:
    """{score, max, checks: [{name, passed, detail}], level, block: reason | None}."""
    from app.db.models import Symbol
    from app.db.models_ai import MarketPlaybook
    st = (ar.structure or {}) if ar is not None else {}
    direction = sig.decision
    want = 1 if direction == "BUY" else -1
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    add("grade", sig.grade == "A+", f"grade {sig.grade}")
    htf = st.get("htf_bias")
    add("higher_timeframe", htf == want, f"higher-timeframe bias {htf if htf is not None else 'unknown'}")
    near = (st.get("key_levels") or {}).get("nearest")
    limit = params.key_level_max_atr or DEFAULT_LEVEL_ATR
    at_level = bool(near) and near["distance_atr"] <= limit
    add("key_level", at_level, f"{near['distance_atr']} ATR from {near['name']} {near['price']}" if near
        else "no key level (history or ATR missing)")
    n = len(sig.strategies or [])
    add("strategy_agreement", n >= 2, f"{n} strategies agree")
    sym = db.get(Symbol, sig.symbol_id)
    pb = db.scalar(select(MarketPlaybook).where(MarketPlaybook.symbol == sym.broker_symbol,
                                                MarketPlaybook.status == "ACTIVE",
                                                MarketPlaybook.valid_until > datetime.now(UTC))
                   .order_by(MarketPlaybook.id.desc()).limit(1)) if sym is not None else None
    add("playbook_bias", pb is not None and pb.bias == direction,
        f"playbook bias {pb.bias}" if pb is not None else "no active playbook")
    conf = sig.calibrated_confidence
    add("confidence", conf is not None and conf >= MIN_CONFIDENCE,
        f"confidence {conf:.2f}" if conf is not None else "confidence not calibrated yet")
    rr = sig.risk_reward
    add("reward_risk", rr is not None and rr >= MIN_RR, f"R:R {rr}" if rr is not None else "R:R unknown")
    score = sum(c["passed"] for c in checks)
    block = None
    if params.key_level_max_atr > 0 and not at_level:
        block = (f"not at a key level ({near['distance_atr']} ATR from {near['name']}, max {params.key_level_max_atr})"
                 if near else "not at a key level (no level available)")
    elif params.min_setup_score > 0 and score < params.min_setup_score:
        missing = ", ".join(c["name"].replace("_", " ") for c in checks if not c["passed"])
        block = f"setup score {score}/{MAX_SCORE} < {params.min_setup_score} (missing: {missing})"
    return {"score": score, "max": MAX_SCORE, "checks": checks, "level": near, "block": block}


def gate_on(params) -> bool:
    return params.min_setup_score > 0 or params.key_level_max_atr > 0
