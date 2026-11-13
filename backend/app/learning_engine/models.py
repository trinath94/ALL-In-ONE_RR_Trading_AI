"""Model training, validation, registry and governance.

Training never promotes a model: a run ends in VALIDATED (passed out-of-sample checks, eligible for an
administrator's approval) or REJECTED (no skill vs the base rate, or too little data). Only models an
authorised user moves to PRODUCTION influence decisions; everything else runs in shadow mode.

Artifacts are pickled estimators produced by this trainer and stored with a SHA-256; they are only
unpickled after the digest is verified.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models_intel import ApprovalRecord, ModelRegistry
from app.learning_engine.datasets import FEATURES, chronological_split
from app.signals.engine import Calibrator

log = logging.getLogger("learning.models")
UTC = timezone.utc
MIN_TRAIN, MIN_TEST = 300, 150
TRANSITIONS = {
    "TRAINED": {"VALIDATED", "REJECTED"},
    "VALIDATED": {"APPROVED", "REJECTED"},
    "APPROVED": {"PRODUCTION", "REJECTED"},
    "PRODUCTION": {"RETIRED"},
    "RETIRED": {"PRODUCTION"},       # rollback target
    "REJECTED": set(),
}


def candidate_models(seed: int = 0) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    cands = {
        "logistic_regression": make_pipeline(StandardScaler(), LogisticRegression(max_iter=500, C=0.5)),
        "random_forest": RandomForestClassifier(n_estimators=200, min_samples_leaf=20, max_depth=6, random_state=seed, n_jobs=2),
        "hist_gradient_boosting": HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                                                 l2_regularization=1.0, random_state=seed),
    }
    try:
        import lightgbm as lgb
        cands["lightgbm"] = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=30,
                                               subsample=0.8, colsample_bytree=0.8, random_state=seed, verbose=-1)
    except ImportError:
        log.info("lightgbm not installed; skipped")
    try:
        import xgboost as xgb
        cands["xgboost"] = xgb.XGBClassifier(n_estimators=200, learning_rate=0.05, max_depth=3, subsample=0.8,
                                             colsample_bytree=0.8, random_state=seed, eval_metric="logloss", n_jobs=2)
    except ImportError:
        log.info("xgboost not installed; skipped")
    return cands


def brier(y, p) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def log_loss(y, p) -> float:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(y, p, bins: int = 10) -> dict:
    y, p = np.asarray(y, float), np.asarray(p, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    rows, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if m.sum() == 0:
            continue
        conf, acc = float(p[m].mean()), float(y[m].mean())
        rows.append({"bin": b, "mean_predicted": round(conf, 4), "observed": round(acc, 4), "count": int(m.sum())})
        ece += m.sum() / len(p) * abs(acc - conf)
    return {"ece": round(float(ece), 4), "bins": rows}


def auc(y, p) -> float | None:
    from sklearn.metrics import roc_auc_score
    y = np.asarray(y)
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


def skill_significance(y, p, base_rate: float, n_boot: int = 500, seed: int = 11) -> dict:
    """Bootstrap 95% lower bounds of (Brier_base - Brier_model) and (AUC - 0.5) on the final test set.
    Positive lower bounds are required before a model can be called skilful — guards against models that
    beat the base rate by chance (multiple-testing / noise)."""
    from sklearn.metrics import roc_auc_score
    y, p = np.asarray(y, float), np.asarray(p, float)
    rng = np.random.default_rng(seed)
    gains, aucs = [], []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        yy, pp = y[i], p[i]
        gains.append(np.mean((base_rate - yy) ** 2) - np.mean((pp - yy) ** 2))
        if 0 < yy.sum() < len(yy):
            aucs.append(roc_auc_score(yy, pp) - 0.5)
    return {"brier_gain_ci95_low": round(float(np.percentile(gains, 2.5)), 5),
            "auc_minus_half_ci95_low": round(float(np.percentile(aucs, 2.5)), 4) if aucs else None}


def feature_baseline(X: pd.DataFrame) -> dict:
    out = {}
    for col in X.columns:
        v = X[col].dropna().to_numpy(float)
        if len(v) < 20:
            continue
        edges = np.unique(np.quantile(v, np.linspace(0, 1, 11)))
        if len(edges) < 3:
            continue
        hist = np.histogram(v, bins=edges)[0] / len(v)
        out[col] = {"edges": edges.tolist(), "proportions": hist.round(5).tolist()}
    return out


def _segments(test: pd.DataFrame, p: np.ndarray, target: str) -> dict:
    out = {}
    df = test.assign(_p=p)
    for col in ("symbol", "timeframe", "strategy", "component", "session_bucket"):
        if col not in df:
            continue
        seg = {}
        for k, g in df.groupby(col):
            if len(g) < 20:
                seg[str(k)] = {"count": int(len(g)), "note": "too few samples"}
                continue
            seg[str(k)] = {"count": int(len(g)), "brier": round(brier(g[target], g._p), 4),
                           "ece": reliability(g[target], g._p, 5)["ece"], "base_rate": round(float(g[target].mean()), 4)}
        out[col] = seg
    return out


def train(db: Session, ds: pd.DataFrame, model_key: str = "prob_tp_before_sl", target: str = "y_tp_before_sl",
          horizon: int = 48, dataset_ref: dict | None = None) -> ModelRegistry:
    data = ds[~ds[target].isna()].copy()
    data[target] = data[target].astype(int)
    data["session_bucket"] = pd.to_datetime(data["time"], utc=True).dt.hour.map(
        lambda h: "ASIA" if h < 7 else ("LONDON" if h < 12 else ("NY" if h < 20 else "LATE")))
    splits = chronological_split(data, horizon)
    tr, va, te = splits["train"], splits["validation"], splits["test"]
    version = (db.scalar(select(func.max(ModelRegistry.version)).where(ModelRegistry.model_key == model_key)) or 0) + 1
    reg = ModelRegistry(model_key=model_key, version=version, algorithm="pending", target=target, features=FEATURES,
                        dataset_ref={**(dataset_ref or {}), "rows": int(len(data)), "train": int(len(tr)),
                                     "validation": int(len(va)), "test": int(len(te)), "embargo_bars": horizon,
                                     "train_end": str(tr.time.max()) if len(tr) else None,
                                     "test_start": str(te.time.min()) if len(te) else None},
                        status="TRAINED")
    if len(tr) < MIN_TRAIN or len(va) < 50 or len(te) < MIN_TEST:
        reg.algorithm, reg.status = "none", "REJECTED"
        reg.notes = (f"LOW DATA: train={len(tr)} (min {MIN_TRAIN}), validation={len(va)}, test={len(te)} "
                     f"(min {MIN_TEST}). Model not trained.")
        db.add(reg)
        db.flush()
        return reg
    Xtr, Xva, Xte = (s[FEATURES].astype(float).fillna(0.0) for s in (tr, va, te))
    ytr, yva, yte = tr[target].to_numpy(), va[target].to_numpy(), te[target].to_numpy()
    results = {}
    for name, est in candidate_models().items():
        try:
            est.fit(Xtr, ytr)
            pva = est.predict_proba(Xva)[:, 1]
            results[name] = {"est": est, "val_log_loss": log_loss(yva, pva), "val_brier": brier(yva, pva), "pva": pva}
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": str(exc)[:200]}
    ok = {k: v for k, v in results.items() if "est" in v}
    best_name = min(ok, key=lambda k: ok[k]["val_log_loss"])
    best = ok[best_name]
    calib = Calibrator.fit(list(best["pva"]), list(yva.astype(bool)))
    raw_te = best["est"].predict_proba(Xte)[:, 1]
    cal_te = np.array([calib.predict(x, min_samples=1)[0] for x in raw_te])
    base_rate = float(ytr.mean())
    b_model, b_clim = brier(yte, cal_te), brier(yte, np.full(len(yte), base_rate))
    bss = 1 - b_model / b_clim if b_clim > 0 else 0.0
    metrics = {
        "selected": best_name,
        "candidates": {k: ({"val_log_loss": round(v["val_log_loss"], 4), "val_brier": round(v["val_brier"], 4)}
                           if "est" in v else v) for k, v in results.items()},
        "test": {"n": int(len(yte)), "brier": round(b_model, 4), "brier_base_rate": round(b_clim, 4),
                 "brier_skill_score": round(bss, 4), "log_loss": round(log_loss(yte, cal_te), 4),
                 "raw_log_loss": round(log_loss(yte, raw_te), 4), "auc": auc(yte, cal_te), "base_rate": round(base_rate, 4),
                 "test_positive_rate": round(float(yte.mean()), 4)},
        "holding_bars": {"median": float(np.nanmedian(data["bars_to_exit"])) if data["bars_to_exit"].notna().any() else None},
        "timeout_rate": round(float(ds["timeout"].mean()), 4) if "timeout" in ds else None,
    }
    sig = skill_significance(yte, cal_te, base_rate)
    metrics["test"].update(sig)
    metrics["n_candidates_tried"] = len(ok)
    calibration = {"test_reliability": reliability(yte, cal_te), "segments": _segments(te, cal_te, target),
                   "calibrator": {"edges": calib.edges, "probs": calib.probs, "counts": calib.counts}}
    blob = pickle.dumps({"model": best["est"], "features": FEATURES})
    reg.algorithm, reg.metrics, reg.calibration = best_name, metrics, calibration
    reg.feature_baseline = feature_baseline(Xtr)
    reg.artifact, reg.artifact_sha256 = blob, hashlib.sha256(blob).hexdigest()
    reg.validated_at = datetime.now(UTC)
    passed = (bss > 0.0 and calibration["test_reliability"]["ece"] < 0.1 and sig["brier_gain_ci95_low"] > 0
              and (sig["auc_minus_half_ci95_low"] or 0) > 0)
    reg.status = "VALIDATED" if passed else "REJECTED"
    reg.notes = ("Passed out-of-sample checks (significant Brier gain and AUC > 0.5 at 95%, ECE < 0.1). "
                 "Awaiting authorised approval; runs in shadow mode." if passed else
                 f"No statistically significant out-of-sample skill (BSS={bss:.4f}, Brier-gain CI low="
                 f"{sig['brier_gain_ci95_low']}, AUC-0.5 CI low={sig['auc_minus_half_ci95_low']}, "
                 f"ECE={calibration['test_reliability']['ece']}). Not eligible for production.")
    db.add(reg)
    db.flush()
    return reg


def load(reg: ModelRegistry):
    if not reg.artifact or hashlib.sha256(reg.artifact).hexdigest() != reg.artifact_sha256:
        raise RuntimeError(f"model {reg.model_key} v{reg.version}: artifact integrity check failed")
    return pickle.loads(reg.artifact)  # noqa: S301 - verified artifact produced by our own trainer


def transition(db: Session, reg: ModelRegistry, to: str, user_id: int | None, notes: str = "") -> ModelRegistry:
    if to not in TRANSITIONS.get(reg.status, set()):
        raise ValueError(f"illegal transition {reg.status} -> {to}")
    if to in ("APPROVED", "PRODUCTION") and user_id is None:
        raise PermissionError("approval and promotion require an authorised user")
    frm = reg.status
    if to == "PRODUCTION":
        cur = db.scalar(select(ModelRegistry).where(ModelRegistry.model_key == reg.model_key,
                                                    ModelRegistry.status == "PRODUCTION"))
        if cur is not None and cur.id != reg.id:
            cur.status = "RETIRED"
            reg.rollback_to = cur.id
            db.add(ApprovalRecord(entity_type="model", entity_id=cur.id, action="retire", from_status="PRODUCTION",
                                  to_status="RETIRED", user_id=user_id, notes=f"replaced by v{reg.version}"))
    if to == "APPROVED":
        reg.approved_by, reg.approved_at = user_id, datetime.now(UTC)
    reg.status = to
    db.add(ApprovalRecord(entity_type="model", entity_id=reg.id, action=to.lower(), from_status=frm, to_status=to,
                          user_id=user_id, notes=notes))
    db.flush()
    return reg


def rollback(db: Session, model_key: str, user_id: int) -> ModelRegistry:
    cur = db.scalar(select(ModelRegistry).where(ModelRegistry.model_key == model_key, ModelRegistry.status == "PRODUCTION"))
    if cur is None or cur.rollback_to is None:
        raise ValueError("no production model with a rollback target")
    prev = db.get(ModelRegistry, cur.rollback_to)
    cur.status = "RETIRED"
    prev.status = "PRODUCTION"
    db.add(ApprovalRecord(entity_type="model", entity_id=cur.id, action="rollback", from_status="PRODUCTION",
                          to_status="RETIRED", user_id=user_id, notes=f"rolled back to v{prev.version}"))
    db.flush()
    return prev


def production(db: Session, model_key: str) -> ModelRegistry | None:
    return db.scalar(select(ModelRegistry).where(ModelRegistry.model_key == model_key, ModelRegistry.status == "PRODUCTION"))


def latest_validated(db: Session, model_key: str) -> ModelRegistry | None:
    return db.scalar(select(ModelRegistry).where(ModelRegistry.model_key == model_key, ModelRegistry.status.in_(
        ["VALIDATED", "APPROVED"])).order_by(ModelRegistry.version.desc()).limit(1))
