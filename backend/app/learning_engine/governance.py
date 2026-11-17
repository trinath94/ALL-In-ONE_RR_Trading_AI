"""Model and strategy governance view (Parts 27-30 Phase 7, Part 30.7).

* **Models:** every PRODUCTION model with its dataset version, validation age (expired after
  RR_MODEL_VALIDATION_MAX_AGE_DAYS) and drift. Unhealthy models feed the health stage of the decision trace. With
  RR_OPPORTUNITY_GATE=enforce, an unhealthy model downgrades BUY/SELL to WAIT: its influence is reduced, never
  silently trusted.
* **Strategies:** a strategy whose recent forward evidence (paper or live, at least 30 trades) has a profit factor
  below 0.8 is listed for **suspension**. Suspending is the administrator's decision (the existing Disable action);
  nothing is disabled automatically.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

MIN_TRADES = 30
SUSPEND_PF = 0.8


def models(db: Session) -> list[dict]:
    from app.intelligence.selection import health
    from app.db.models_intel import ModelRegistry
    hl = {(m["model"], m["version"]): m for m in health(db)["models"]}
    out = []
    for m in db.scalars(select(ModelRegistry).where(ModelRegistry.status == "PRODUCTION").order_by(ModelRegistry.model_key)):
        h = hl.get((m.model_key, m.version), {})
        out.append({"model": m.model_key, "version": m.version, "dataset_version": (m.dataset_ref or {}).get("dataset_version"),
                    "trained_at": m.trained_at, "validated_at": m.validated_at, "healthy": h.get("healthy"),
                    "validation_expired": h.get("validation_expired"), "detail": h.get("detail")})
    return out


def strategy_suspensions(db: Session) -> list[dict]:
    from app.db.models import Strategy, StrategyPerformance
    rows = db.execute(select(Strategy.code, Strategy.enabled, func.sum(StrategyPerformance.trades),
                             func.sum(StrategyPerformance.gross_profit), func.sum(StrategyPerformance.gross_loss))
                      .join(StrategyPerformance, StrategyPerformance.strategy_id == Strategy.id)
                      .where(StrategyPerformance.source.in_(["PAPER", "LIVE"]))
                      .group_by(Strategy.code, Strategy.enabled)).all()
    out = []
    for code, enabled, n, gp, gl in rows:
        n, gp, gl = int(n or 0), float(gp or 0), float(gl or 0)
        pf = gp / gl if gl else None
        if n >= MIN_TRADES and pf is not None and pf < SUSPEND_PF:
            out.append({"strategy": code, "enabled": enabled, "trades": n, "profit_factor": round(pf, 3),
                        "recommendation": "suspend (administrator decision)",
                        "reason": f"forward profit factor {pf:.2f} < {SUSPEND_PF} over {n} paper/live trades"})
    return out
