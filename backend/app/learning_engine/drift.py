"""Model, feature, prediction and strategy-performance drift monitoring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClosedPosition
from app.db.models_intel import DriftEvent, ModelPrediction, ModelRegistry, TradeFeatures
from app.learning_engine.performance import bootstrap_ci, learnable
from app.notifications.service import notify

UTC = timezone.utc
PSI_WARN, PSI_ALERT = 0.1, 0.25


def psi(expected: list[float], actual: list[float]) -> float:
    e = np.clip(np.asarray(expected, float), 1e-4, None)
    a = np.clip(np.asarray(actual, float), 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def feature_drift(reg: ModelRegistry, recent: list[dict]) -> list[dict]:
    out = []
    if len(recent) < 50:
        return out
    for feat, base in (reg.feature_baseline or {}).items():
        vals = np.array([r[feat] for r in recent if feat in r and r[feat] is not None], float)
        if len(vals) < 50:
            continue
        edges = np.array(base["edges"])
        hist = np.histogram(np.clip(vals, edges[0], edges[-1]), bins=edges)[0] / len(vals)
        v = psi(base["proportions"], hist)
        if v >= PSI_WARN:
            out.append({"feature": feat, "psi": round(v, 4), "severity": "critical" if v >= PSI_ALERT else "warning"})
    return out


def check(db: Session, days: int = 14) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)
    events = []
    for reg in db.scalars(select(ModelRegistry).where(ModelRegistry.status.in_(["PRODUCTION", "VALIDATED", "APPROVED"]))):
        preds = db.scalars(select(ModelPrediction).where(ModelPrediction.model_id == reg.id,
                                                         ModelPrediction.created_at >= since)).all()
        for f in feature_drift(reg, [p.features for p in preds]):
            events.append(DriftEvent(scope="feature", subject=f"{reg.model_key}:v{reg.version}", metric=f"psi:{f['feature']}",
                                     value=f["psi"], threshold=PSI_WARN, severity=f["severity"]))
        test = (reg.metrics or {}).get("test") or {}
        if len(preds) >= 50 and test.get("test_positive_rate") is not None:
            mean_p = float(np.mean([p.probability for p in preds if p.probability is not None] or [np.nan]))
            shift = abs(mean_p - test["test_positive_rate"])
            if np.isfinite(shift) and shift > 0.1:
                events.append(DriftEvent(scope="prediction", subject=f"{reg.model_key}:v{reg.version}", metric="mean_probability_shift",
                                         value=round(shift, 4), threshold=0.1, severity="warning",
                                         details={"recent_mean": mean_p, "validation_rate": test["test_positive_rate"]}))
        # realised calibration on linked closed trades (performance drift)
        linked = db.execute(select(ModelPrediction.probability, TradeFeatures.outcome)
                            .join(TradeFeatures, TradeFeatures.signal_id == ModelPrediction.signal_id)
                            .where(ModelPrediction.model_id == reg.id, TradeFeatures.outcome.in_(["WIN", "LOSS"]))).all()
        if len(linked) >= 30 and test.get("brier") is not None:
            b = float(np.mean([(p - (o == "WIN")) ** 2 for p, o in linked if p is not None]))
            if b > test["brier"] * 1.25:
                events.append(DriftEvent(scope="performance", subject=f"{reg.model_key}:v{reg.version}", metric="live_brier",
                                         value=round(b, 4), threshold=round(test["brier"] * 1.25, 4), severity="critical",
                                         details={"n": len(linked), "validation_brier": test["brier"]}))
    events += strategy_deterioration(db)
    for e in events:
        db.add(e)
        if e.severity == "critical":
            notify(db, "DRAWDOWN_WARNING", f"Drift: {e.subject} {e.metric}", f"value {e.value} > threshold {e.threshold}",
                   {"scope": e.scope})
    db.flush()
    return {"events": len(events)}


def strategy_deterioration(db: Session, window: int = 30) -> list[DriftEvent]:
    out = []
    rows = db.execute(select(ClosedPosition.strategy_codes, ClosedPosition.profit, ClosedPosition.commission,
                             ClosedPosition.swap, ClosedPosition.mode).where(learnable()).order_by(ClosedPosition.closed_at)).all()
    per: dict[tuple[str, str], list[float]] = {}
    for codes, p, c, s, mode in rows:
        for code in codes or []:
            per.setdefault((code, mode or "DEMO"), []).append(float(p) + float(c or 0) + float(s or 0))
    for (code, mode), nets in per.items():
        if len(nets) < 2 * window:
            continue
        hist, recent = np.array(nets[:-window]), np.array(nets[-window:])
        lo, hi = bootstrap_ci(recent, lambda x: float(x.mean()))
        if hist.mean() > 0 and hi is not None and hi < 0:
            out.append(DriftEvent(scope="performance", subject=f"{mode}:{code}", metric="expectancy_last_window",
                                  value=round(float(recent.mean()), 2), threshold=0.0, severity="critical",
                                  details={"historical_expectancy": round(float(hist.mean()), 2), "ci95": [lo, hi],
                                           "window": window}))
    return out
