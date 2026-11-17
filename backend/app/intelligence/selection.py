"""Strategy selection decision (Parts 27-30 Phase 6, Part 29.2) and model/provider health (Part 29.9).

**Selection.** For each analysis with setups, the strategies supporting the engine's direction are assessed on the
evidence that already exists:
* out-of-sample, walk-forward, paper and live results (never in-sample backtests): trades, win rate, profit factor,
  expectancy per trade;
* regime suitability (their declared regimes against the current regime and tags);
* category diversity.

The decision is one of SELECT, COMBINATION, WAIT, REJECT or NO_TRADE, with every metric recorded:
* **NO_TRADE / WAIT:** the engine decided no trade / wait;
* **REJECT:** the supporting strategies with sufficient evidence (at least 30 trades) all have a profit factor below 1;
* **COMBINATION:** two or more regime-suitable supporting strategies from different categories;
* **SELECT:** otherwise, led by the best-evidenced supporting strategy.

It also says whether the evidence is ``proven`` (some supporting strategy has enough trades) or ``unproven``. It is
recorded in the decision trace; with RR_OPPORTUNITY_GATE=enforce, REJECT downgrades BUY/SELL to WAIT.

**Health.**
* Production models are unhealthy if a drift event names them in the last 24 h.
* AI providers are unhealthy if they are cooling down after failures.
* An unhealthy provider already leaves candidates unreviewed (never auto-executed). With RR_OPPORTUNITY_GATE=enforce,
  an unhealthy production model downgrades BUY/SELL to WAIT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

MIN_TRADES = 30


def assess(res, stats: dict, metas: dict, regime: str, tags: list[str]) -> dict:
    if res.decision == "NO_TRADE" or not res.direction:
        return {"decision": "NO_TRADE", "reason": "signal engine: no trade", "strategies": []}
    codes = sorted({x["strategy"] for x in res.strategies if x.get("direction") in (None, res.direction)})
    rows = []
    labels = {regime, *tags}
    for c in codes:
        st = stats.get(c) or {}
        n = int(st.get("trades") or 0)
        gp, gl = float(st.get("gp") or 0), float(st.get("gl") or 0)
        m = metas.get(c)
        regs = list(getattr(m, "regimes", []) or [])
        rows.append({"strategy": c, "category": getattr(m, "category", None), "trades": n,
                     "win_rate": round(st["wins"] / n, 4) if n else None,
                     "profit_factor": round(gp / gl, 3) if gl else None,
                     "expectancy": round((gp - gl) / n, 4) if n else None,
                     "sufficient": n >= MIN_TRADES,
                     "regime_suitable": (not regs) or bool(labels & set(regs))})
    proven = [r for r in rows if r["sufficient"]]
    evidence = "proven" if proven else "unproven"
    if res.decision == "WAIT":
        return {"decision": "WAIT", "reason": "signal engine: wait for confirmation", "evidence": evidence,
                "strategies": rows}
    if proven and all((r["profit_factor"] or 0) < 1 for r in proven):
        return {"decision": "REJECT", "reason": "every supporting strategy with enough evidence has a profit factor below 1",
                "evidence": evidence, "strategies": rows}
    suitable = [r for r in rows if r["regime_suitable"]]
    if len(suitable) >= 2 and len({r["category"] for r in suitable}) >= 2:
        return {"decision": "COMBINATION",
                "reason": f"{len(suitable)} regime-suitable strategies from {len({r['category'] for r in suitable})} categories",
                "evidence": evidence, "strategies": rows}
    lead = max(rows, key=lambda r: (r["sufficient"], r["profit_factor"] or 0, r["trades"])) if rows else None
    return {"decision": "SELECT", "reason": f"led by {lead['strategy']}" if lead else "no supporting strategy",
            "lead": lead["strategy"] if lead else None, "evidence": evidence, "strategies": rows}


def health(db: Session) -> dict:
    from app.db.models_intel import DriftEvent, ModelRegistry
    since = datetime.now(UTC) - timedelta(hours=24)
    prod = db.scalars(select(ModelRegistry).where(ModelRegistry.status == "PRODUCTION")).all()
    drift = {d.subject for d in db.scalars(select(DriftEvent).where(DriftEvent.created_at >= since))}
    models = []
    for m in prod:
        names = {getattr(m, "model_key", None), f"{getattr(m, 'model_key', '')}:v{getattr(m, 'version', '')}"}
        bad = bool(names & drift)
        models.append({"model": getattr(m, "model_key", None), "version": getattr(m, "version", None),
                       "healthy": not bad, "detail": "drift event in the last 24 h" if bad else "no drift event in 24 h"})
    providers = []
    try:
        from app.db.models_ai import AIProvider
        from app.ai.gateway import _cooling
        for p in db.scalars(select(AIProvider).where(AIProvider.enabled.is_(True))):
            cool = _cooling(p.name)
            providers.append({"provider": p.name, "healthy": not cool,
                              "detail": "cooling down after failures or a rate limit" if cool else "available"})
    except Exception as exc:  # noqa: BLE001
        providers.append({"provider": "?", "healthy": None, "detail": f"unknown: {str(exc)[:100]}"})
    return {"models": models, "providers": providers,
            "models_healthy": all(m["healthy"] for m in models),
            "providers_healthy": all(p["healthy"] is not False for p in providers)}
