"""Model training, validation, registry and governance.

Training never promotes a model: a run ends in VALIDATED (passed out-of-sample checks, eligible for an
administrator's approval) or REJECTED (no skill vs the base rate, or too little data). Only models an
authorised user moves to PRODUCTION influence decisions; everything else runs in shadow mode.

Artifacts are pickled estimators produced by this trainer and stored with a SHA-256; they are only
unpickled after the digest is verified.

RR Trading One Phase 11 (specification Part 11, Phase 1 gap 7.8):
* artefacts are also HMAC-signed with RR_MODEL_SIGNING_KEY; with the key configured, an artefact without a valid
  signature is never unpickled, approved, promoted or rolled back to (a SHA-256 stored next to the artefact proves
  integrity, not origin: anyone able to write the row could replace both);
* a candidate must also show skill in a walk-forward evaluation (expanding windows with embargo), and must not be
  worse than the current production model on the same held-out data.
"""
from __future__ import annotations

import hashlib
import hmac
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
WF_FOLDS = 4
# Minimum mean walk-forward Brier skill (1% better than the base rate). Without an effect-size floor, pure noise can
# pass "skill > 0" by chance: a random forest on a no-signal dataset reached 0.0021 with 3 of 4 folds barely positive.
WF_MIN_SKILL = 0.01
USABLE_SIGNATURES = ("valid", "not_enforced")


class ModelIntegrityError(RuntimeError):
    pass


def _signing_key() -> str:
    from app.core.config import get_settings
    return get_settings().model_signing_key


def sign(blob: bytes) -> str | None:
    key = _signing_key()
    return hmac.new(key.encode(), blob, hashlib.sha256).hexdigest() if key else None


def signature_state(reg: ModelRegistry) -> str:
    """valid | not_enforced (no signing key configured: SHA-256 only, the original behaviour) | unsigned | invalid |
    no_artifact."""
    if not reg.artifact:
        return "no_artifact"
    if hashlib.sha256(reg.artifact).hexdigest() != reg.artifact_sha256:
        return "invalid"
    if not _signing_key():
        return "not_enforced"
    if not reg.artifact_hmac:
        return "unsigned"
    return "valid" if hmac.compare_digest(sign(reg.artifact), reg.artifact_hmac) else "invalid"


def require_usable(reg: ModelRegistry) -> None:
    st = signature_state(reg)
    if st not in USABLE_SIGNATURES:
        raise ModelIntegrityError(f"model {reg.model_key} v{reg.version}: artefact signature {st}; it is not used. "
                                  "Retrain to obtain a signed model.")
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
    except Exception as exc:  # noqa: BLE001 - ImportError, or OSError when its native library (libgomp) is missing
        log.warning("lightgbm unavailable (%s); skipped", exc)
    try:
        import xgboost as xgb
        cands["xgboost"] = xgb.XGBClassifier(n_estimators=200, learning_rate=0.05, max_depth=3, subsample=0.8,
                                             colsample_bytree=0.8, random_state=seed, eval_metric="logloss", n_jobs=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("xgboost unavailable (%s); skipped", exc)
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


def walk_forward(data: pd.DataFrame, target: str, template, horizon: int, folds: int = WF_FOLDS) -> dict:
    """Expanding-window walk-forward: the period is cut into folds+1 blocks; for each fold the selected algorithm is
    retrained from scratch on everything before the block (minus an embargo of `horizon` bars) and scored on the
    block. Raw probabilities against the training base rate (no per-fold recalibration). Rule: at least 3 scored
    folds, mean Brier skill of at least WF_MIN_SKILL, and at most one fold without skill."""
    from sklearn.base import clone
    d = data.sort_values("bar_index").reset_index(drop=True)
    bmin, bmax = float(d.bar_index.min()), float(d.bar_index.max())
    edges = [bmin + (bmax - bmin) * k / (folds + 1) for k in range(folds + 2)]
    out = []
    for k in range(1, folds + 1):
        tr = d[d.bar_index + horizon < edges[k]]
        te = d[(d.bar_index >= edges[k]) & ((d.bar_index < edges[k + 1]) if k < folds else (d.bar_index <= bmax))]
        row = {"fold": k, "train": int(len(tr)), "test": int(len(te)),
               "train_end": str(tr.time.max()) if len(tr) else None, "test_start": str(te.time.min()) if len(te) else None}
        if len(tr) < 100 or len(te) < 30 or tr[target].nunique() < 2:
            out.append({**row, "skipped": "too few samples"})
            continue
        est = clone(template)
        est.fit(tr[FEATURES].astype(float).fillna(0.0), tr[target].to_numpy())
        p = est.predict_proba(te[FEATURES].astype(float).fillna(0.0))[:, 1]
        y = te[target].to_numpy()
        b, bb = brier(y, p), brier(y, np.full(len(y), float(tr[target].mean())))
        out.append({**row, "brier": round(b, 4), "brier_base_rate": round(bb, 4),
                    "brier_skill_score": round(1 - b / bb, 4) if bb > 0 else 0.0})
    scored = [f for f in out if "brier_skill_score" in f]
    positive = sum(1 for f in scored if f["brier_skill_score"] > 0)
    mean = round(float(np.mean([f["brier_skill_score"] for f in scored])), 4) if scored else None
    return {"folds": out, "scored": len(scored), "positive": positive, "mean_brier_skill": mean,
            "embargo_bars": horizon,
            "rule": f"at least 3 scored folds, mean Brier skill >= {WF_MIN_SKILL}, at most one fold without skill",
            "passed": len(scored) >= 3 and (mean or 0) >= WF_MIN_SKILL and positive >= len(scored) - 1}


def compare_with_production(db: Session, model_key: str, Xte: pd.DataFrame, yte, cal_te) -> dict:
    """The candidate against the model currently in production, on the candidate's own held-out test data."""
    prod = production(db, model_key)
    if prod is None:
        return {"production": None, "passed": True, "note": "no production model: compared with the base rate only"}
    name = f"v{prod.version} ({prod.algorithm})"
    if list(prod.features or []) != list(FEATURES):
        return {"production": name, "passed": True, "note": "feature set differs: not comparable on this test data"}
    try:
        art = load(prod)
    except ModelIntegrityError as exc:
        return {"production": name, "passed": True, "note": f"production model not usable ({exc}); not compared"}
    cal = (prod.calibration or {}).get("calibrator") or {}
    calib = Calibrator(cal.get("edges", []), cal.get("probs", []), cal.get("counts", []))
    raw = art["model"].predict_proba(Xte)[:, 1]
    p_prod = np.array([calib.predict(x, min_samples=1)[0] if calib.counts else x for x in raw], float)
    b_prod, b_cand = brier(yte, p_prod), brier(yte, cal_te)
    return {"production": name, "brier_production": round(b_prod, 4), "brier_candidate": round(b_cand, 4),
            "passed": b_cand <= b_prod,
            "note": "candidate must not be worse than production on the same held-out data"}


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
    metrics["walk_forward"] = walk_forward(data, target, best["est"], horizon)
    metrics["baseline_comparison"] = compare_with_production(db, model_key, Xte, yte, cal_te)
    calibration = {"test_reliability": reliability(yte, cal_te), "segments": _segments(te, cal_te, target),
                   "calibrator": {"edges": calib.edges, "probs": calib.probs, "counts": calib.counts}}
    blob = pickle.dumps({"model": best["est"], "features": FEATURES})
    reg.algorithm, reg.metrics, reg.calibration = best_name, metrics, calibration
    reg.feature_baseline = feature_baseline(Xtr)
    reg.artifact, reg.artifact_sha256 = blob, hashlib.sha256(blob).hexdigest()
    reg.artifact_hmac = sign(blob)
    reg.validated_at = datetime.now(UTC)
    holdout_ok = (bss > 0.0 and calibration["test_reliability"]["ece"] < 0.1 and sig["brier_gain_ci95_low"] > 0
                  and (sig["auc_minus_half_ci95_low"] or 0) > 0)
    wf_ok, base_ok = metrics["walk_forward"]["passed"], metrics["baseline_comparison"]["passed"]
    passed = holdout_ok and wf_ok and base_ok
    reg.status = "VALIDATED" if passed else "REJECTED"
    failed = []
    if not holdout_ok:
        failed.append(f"no statistically significant out-of-sample skill (BSS={bss:.4f}, Brier-gain CI low="
                      f"{sig['brier_gain_ci95_low']}, AUC-0.5 CI low={sig['auc_minus_half_ci95_low']}, "
                      f"ECE={calibration['test_reliability']['ece']})")
    if not wf_ok:
        failed.append(f"walk-forward: {metrics['walk_forward']['positive']} of {metrics['walk_forward']['scored']} "
                      f"folds with skill (rule: {metrics['walk_forward']['rule']})")
    if not base_ok:
        failed.append(f"worse than the production model on the same test data "
                      f"({metrics['baseline_comparison'].get('brier_candidate')} vs "
                      f"{metrics['baseline_comparison'].get('brier_production')} Brier)")
    reg.notes = ("Passed out-of-sample checks (significant Brier gain and AUC > 0.5 at 95%, ECE < 0.1), the walk-forward "
                 "evaluation and the comparison with the current production model. Awaiting authorised approval; "
                 "runs in shadow mode." if passed else "Rejected: " + "; ".join(failed) + ". Not eligible for production.")
    db.add(reg)
    db.flush()
    return reg


def load(reg: ModelRegistry):
    require_usable(reg)            # SHA-256 always; HMAC whenever a signing key is configured
    return pickle.loads(reg.artifact)  # noqa: S301 - verified artifact produced by our own trainer


def transition(db: Session, reg: ModelRegistry, to: str, user_id: int | None, notes: str = "") -> ModelRegistry:
    if to not in TRANSITIONS.get(reg.status, set()):
        raise ValueError(f"illegal transition {reg.status} -> {to}")
    if to in ("APPROVED", "PRODUCTION") and user_id is None:
        raise PermissionError("approval and promotion require an authorised user")
    if to in ("APPROVED", "PRODUCTION"):
        try:
            require_usable(reg)
        except ModelIntegrityError as exc:
            raise ValueError(str(exc)) from None
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
    try:
        require_usable(prev)
    except ModelIntegrityError as exc:
        raise ValueError(f"rollback refused: {exc}") from None
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
