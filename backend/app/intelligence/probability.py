"""Probability & uncertainty engine (inference).

Separates: raw model score, calibrated probability, uncertainty (95% Wilson half-width of the
calibration bin), and the historical sample size behind it. Expected R is computed **net of costs**:
    E[R] = p * RR - (1 - p) * 1 - cost_R,   cost_R = (spread + commission + slippage) / initial risk
Only a PRODUCTION model influences decisions. A VALIDATED model is evaluated in shadow mode and labelled
so. No model -> ``available: False``. A probability never authorises a trade on its own.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.db.models_intel import ModelPrediction, ModelRegistry
from app.learning_engine import models as mreg
from app.learning_engine.datasets import COMPONENTS, FEATURES
from app.learning_engine.performance import wilson
from app.signals.engine import Calibrator
from app.strategies.quantitative import FEATURES as BASE_FEATURES
from app.strategies.quantitative import ml_features
from app.analysis import indicators as ind

MODEL_KEY = "prob_tp_before_sl"
LOW_DATA_N = 50
_cache: dict[tuple[int, str], dict] = {}


def signal_features(df: pd.DataFrame, sig: dict, n_confluent: int) -> pd.DataFrame:
    base = ml_features(df).iloc[[-1]][BASE_FEATURES]
    atr = float(ind.atr(df).iloc[-1])
    atr_rank = float((ind.atr(df) / df["close"]).rolling(200, min_periods=50).rank(pct=True).iloc[-1])
    entry, stop, tp = sig["entry"], sig["stop_loss"], sig["take_profit"]
    risk = abs(entry - stop)
    ts = df.index[-1]
    row = base.iloc[0].to_dict()
    row.update({"dir": 1 if sig["direction"] == "BUY" else -1, "rr": abs(tp - entry) / risk if risk else 0.0,
                "stop_atr": risk / atr if atr else 0.0, "strength": sig.get("strength", 0.5),
                "hour_sin": np.sin(2 * np.pi * ts.hour / 24), "hour_cos": np.cos(2 * np.pi * ts.hour / 24),
                "adx14": float(ind.adx(df)["adx"].iloc[-1]) / 100, "atr_rank": atr_rank, "n_confluent": n_confluent})
    for comp in COMPONENTS:
        row[f"comp_{comp}"] = 1.0 if sig.get("component") == comp else 0.0
    return pd.DataFrame([row])[FEATURES].astype(float).fillna(0.0)


def _artifact(reg: ModelRegistry) -> dict:
    key = (reg.id, reg.artifact_sha256 or "")
    if key not in _cache:
        art = mreg.load(reg)
        cal = reg.calibration.get("calibrator", {})
        art["calibrator"] = Calibrator(cal.get("edges", []), cal.get("probs", []), cal.get("counts", []))
        _cache[key] = art
    return _cache[key]


def estimate(db: Session, df: pd.DataFrame, sig: dict, n_confluent: int, cost_points: float, point: float,
             symbol: str, timeframe: str, signal_id: int | None = None, record: bool = True) -> dict:
    reg = mreg.production(db, MODEL_KEY)
    shadow = False
    if reg is None:
        reg = mreg.latest_validated(db, MODEL_KEY)
        shadow = True
    if reg is None:
        return {"available": False, "status": "NO_VALIDATED_MODEL",
                "note": "No validated probability model yet — probability unavailable (not estimated)."}
    art = _artifact(reg)
    X = signal_features(df, sig, n_confluent)
    raw = float(art["model"].predict_proba(X)[:, 1][0])
    cal: Calibrator = art["calibrator"]
    p, n_total = cal.predict(raw, min_samples=1)
    i = int(np.clip(np.searchsorted(cal.edges, raw, side="right") - 1, 0, max(len(cal.counts) - 1, 0))) if cal.counts else 0
    bin_n = cal.counts[i] if cal.counts else 0
    lo, hi = wilson(int(round((p or 0) * bin_n)), bin_n) if bin_n else (None, None)
    unc = round((hi - lo) / 2, 4) if lo is not None else None
    risk = abs(sig["entry"] - sig["stop_loss"])
    rr = abs(sig["take_profit"] - sig["entry"]) / risk if risk else 0.0
    cost_r = cost_points * point / risk if risk else 0.0
    exp_r = (p * rr - (1 - p) - cost_r) if p is not None else None
    out = {
        "available": True, "shadow": shadow, "model": f"{reg.model_key}:v{reg.version}", "algorithm": reg.algorithm,
        "model_status": reg.status, "raw_score": round(raw, 4),
        "tp_before_sl_probability": round(p, 4) if p is not None else None,
        "sl_before_tp_probability": round(1 - p, 4) if p is not None else None,
        "uncertainty_95": unc, "calibration_bin_samples": bin_n, "historical_samples": n_total,
        "low_data_confidence": bin_n < LOW_DATA_N, "expected_r_net": round(exp_r, 3) if exp_r is not None else None,
        "cost_r": round(cost_r, 4), "rr": round(rr, 3),
        "expected_holding_bars": (reg.metrics.get("holding_bars") or {}).get("median"),
        "test_brier_skill": (reg.metrics.get("test") or {}).get("brier_skill_score"),
        "test_ece": (reg.calibration.get("test_reliability") or {}).get("ece"),
    }
    if record:
        db.add(ModelPrediction(model_id=reg.id, signal_id=signal_id, symbol=symbol, timeframe=timeframe, raw_score=raw,
                               probability=p, uncertainty=unc, shadow=shadow,
                               features={k: round(float(v), 6) for k, v in X.iloc[0].items()}))
    return out
