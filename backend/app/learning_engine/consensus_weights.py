"""Fit specialist weights from realised outcomes (out-of-sample validated, never auto-promoted)."""
from __future__ import annotations

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models_intel import ConsensusDecision, ModelRegistry, TradeFeatures
from app.intelligence.consensus import SPECIALISTS
from app.learning_engine.models import brier, log_loss, skill_significance

MIN_ROWS = 200
VOTERS = [s for s in SPECIALISTS if s not in ("RISK", "CLAUDE")]


def dataset(db: Session) -> tuple[np.ndarray, np.ndarray]:
    rows = db.execute(select(ConsensusDecision, TradeFeatures.outcome)
                      .join(TradeFeatures, TradeFeatures.signal_id == ConsensusDecision.signal_id)
                      .where(TradeFeatures.outcome.in_(["WIN", "LOSS"])).order_by(ConsensusDecision.id)).all()
    X, y = [], []
    for cd, outcome in rows:
        trade_dir = next((s["decision"] for s in cd.specialists if s["decision"] in ("BUY", "SELL")), None)
        vec = []
        for name in VOTERS:
            s = next((x for x in cd.specialists if x["specialist"] == name), None)
            if not s or s["decision"] not in ("BUY", "SELL"):
                vec.append(-0.5 * (s or {}).get("confidence", 0) if s and s["decision"] == "NO_TRADE" else 0.0)
            else:
                vec.append(s["confidence"] * (1 if s["decision"] == trade_dir else -1))
        X.append(vec)
        y.append(1 if outcome == "WIN" else 0)
    return np.array(X, float), np.array(y, int)


def specialist_performance(db: Session) -> dict:
    X, y = dataset(db)
    out = {}
    for j, name in enumerate(VOTERS):
        if len(y) == 0:
            out[name] = {"aligned_trades": 0}
            continue
        m = X[:, j] > 0
        out[name] = {"aligned_trades": int(m.sum()), "win_rate_when_aligned": round(float(y[m].mean()), 4) if m.sum() else None,
                     "win_rate_when_silent": round(float(y[X[:, j] == 0].mean()), 4) if (X[:, j] == 0).sum() else None}
    return out


def fit(db: Session) -> ModelRegistry:
    from sklearn.linear_model import LogisticRegression
    X, y = dataset(db)
    version = (db.scalar(select(func.max(ModelRegistry.version)).where(ModelRegistry.model_key == "consensus_weights")) or 0) + 1
    reg = ModelRegistry(model_key="consensus_weights", version=version, algorithm="l2_logistic_regression",
                        target="trade_win", features=VOTERS, dataset_ref={"rows": int(len(y))}, status="TRAINED")
    if len(y) < MIN_ROWS or len(set(y)) < 2:
        reg.status, reg.notes = "REJECTED", f"LOW DATA: {len(y)} outcome-linked consensus records (min {MIN_ROWS})."
        db.add(reg)
        db.flush()
        return reg
    cut = int(len(y) * 0.7)
    lr = LogisticRegression(C=0.5, max_iter=500).fit(X[:cut], y[:cut])
    p = lr.predict_proba(X[cut:])[:, 1]
    base = float(y[:cut].mean())
    sig = skill_significance(y[cut:], p, base)
    coef = np.clip(lr.coef_[0], 0, None)
    weights = {n: round(float(c / coef.sum()), 4) if coef.sum() > 0 else 0.0 for n, c in zip(VOTERS, coef)}
    reg.metrics = {"weights": weights, "raw_coefficients": dict(zip(VOTERS, np.round(lr.coef_[0], 4).tolist())),
                   "test": {"n": int(len(y) - cut), "log_loss": round(log_loss(y[cut:], p), 4), "brier": round(brier(y[cut:], p), 4),
                            "brier_base_rate": round(brier(y[cut:], np.full(len(p), base)), 4), **sig}}
    ok = sig["brier_gain_ci95_low"] > 0 and coef.sum() > 0
    reg.status = "VALIDATED" if ok else "REJECTED"
    reg.notes = ("Weights show significant out-of-sample value; awaiting authorised approval." if ok else
                 "Weights add no significant out-of-sample value; consensus stays in prior mode.")
    db.add(reg)
    db.flush()
    return reg
