"""Experimental strategy lifecycle, versioning, validation orchestration and approvals.

DISCOVERED -> DEVELOPING -> EXPERIMENTAL -> BACKTESTING -> VALIDATION -> DEMO_TESTING -> APPROVAL_REQUIRED
-> APPROVED -> PRODUCTION, with REJECTED / DISABLED reachable from any stage.

Gates are evidence-based: EXPERIMENTAL requires static validation + sandboxed unit tests; VALIDATION
requires the sandboxed out-of-sample/walk-forward/Monte-Carlo/cost checks; APPROVAL_REQUIRED requires a
DEMO forward (shadow) test with enough resolved signals. APPROVED and PRODUCTION need an authorised user.
"PRODUCTION" records approval for a developer to port the code into a code-reviewed registry module —
experimental code is never executed by the production trading process.
"""
from __future__ import annotations

import glob
import os
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import StrategyExperiment
from app.db.models_intel import ApprovalRecord
from app.intelligence.fingerprint import proposal_overlap
from app.strategies.experimental import sandbox
from app.strategies.experimental.validator import validate as static_validate

UTC = timezone.utc
STAGES = ["DISCOVERED", "DEVELOPING", "EXPERIMENTAL", "BACKTESTING", "VALIDATION", "DEMO_TESTING", "APPROVAL_REQUIRED",
          "APPROVED", "PRODUCTION", "REJECTED", "DISABLED"]
LEGACY = {"RESEARCH": "DISCOVERED", "IMPLEMENTATION": "DEVELOPING", "UNIT_TESTING": "EXPERIMENTAL",
          "OUT_OF_SAMPLE": "VALIDATION", "WALK_FORWARD": "VALIDATION", "PAPER_TRADING": "DEMO_TESTING",
          "ADMIN_APPROVAL": "APPROVAL_REQUIRED"}
MANUAL_ONLY = {"APPROVED", "PRODUCTION"}           # need an authorised user
AUTO_GATED = {"EXPERIMENTAL", "VALIDATION", "APPROVAL_REQUIRED"}   # need recorded evidence
MIN_FORWARD_SIGNALS = 20
LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")


def normalise(stage: str) -> str:
    return LEGACY.get(stage, stage)


def _record(db: Session, exp: StrategyExperiment, to: str, user_id: int | None, notes: str, action: str = "stage") -> None:
    db.add(ApprovalRecord(entity_type="experiment", entity_id=exp.id, action=action, from_status=exp.stage,
                          to_status=to, user_id=user_id, notes=notes[:2000]))
    exp.stage = to


def transition(db: Session, exp: StrategyExperiment, to: str, user_id: int | None, notes: str = "") -> StrategyExperiment:
    to = normalise(to)
    cur = normalise(exp.stage)
    if to not in STAGES:
        raise ValueError(f"unknown stage {to}")
    if to in ("REJECTED", "DISABLED"):
        _record(db, exp, to, user_id, notes)
        return exp
    if cur in ("REJECTED", "DISABLED"):
        raise ValueError(f"experiment is {cur}; create a new version to continue")
    if STAGES.index(to) != STAGES.index(cur) + 1:
        raise ValueError(f"stages advance one at a time: {cur} -> {STAGES[STAGES.index(cur) + 1]}")
    v = exp.validation or {}
    if to in MANUAL_ONLY and user_id is None:
        raise PermissionError(f"{to} requires an authorised user")
    if to == "DEVELOPING" and not exp.source_code:
        raise ValueError("attach strategy source before DEVELOPING")
    if to == "EXPERIMENTAL" and not (v.get("unit") or {}).get("passed"):
        raise ValueError("sandboxed unit tests have not passed")
    if to == "VALIDATION" and not (v.get("validation") or {}).get("validation_passed"):
        raise ValueError("out-of-sample validation has not passed")
    if to == "APPROVAL_REQUIRED":
        fwd = v.get("forward") or {}
        if fwd.get("resolved", 0) < MIN_FORWARD_SIGNALS:
            raise ValueError(f"DEMO forward test needs >= {MIN_FORWARD_SIGNALS} resolved signals (has {fwd.get('resolved', 0)})")
    _record(db, exp, to, user_id, notes)
    db.flush()
    return exp


def attach_source(db: Session, exp: StrategyExperiment, src: str, user_id: int | None, note: str = "") -> dict:
    check = static_validate(src)
    v = dict(exp.validation or {})
    hist = list(v.get("versions", []))
    if exp.source_code:
        hist.append({"version": exp.version, "source_hash": exp.source_hash, "source": exp.source_code,
                     "replaced_at": datetime.now(UTC).isoformat()})
    v["versions"] = hist[-20:]
    v["static"] = check
    v.pop("unit", None)
    v.pop("validation", None)
    v.pop("forward", None)
    exp.validation = v
    exp.source_code, exp.source_hash = src, check.get("source_hash")
    exp.version = (exp.version or 0) + 1
    if check.get("code"):
        clash = db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == check["code"], StrategyExperiment.id != exp.id))
        if clash:
            check["ok"] = False
            check.setdefault("errors", []).append(f"code {check['code']} already used by experiment {clash.id}")
        else:
            exp.code = check["code"]
    exp.category = check.get("category") or exp.category
    target = "DEVELOPING"
    if normalise(exp.stage) not in ("DISCOVERED", "DEVELOPING"):
        db.add(ApprovalRecord(entity_type="experiment", entity_id=exp.id, action="new_version", from_status=exp.stage,
                              to_status=target, user_id=user_id, notes=note or "source replaced; validation reset"))
    exp.stage = target
    db.flush()
    return check


def rollback(db: Session, exp: StrategyExperiment, user_id: int) -> StrategyExperiment:
    hist = list((exp.validation or {}).get("versions", []))
    if not hist:
        raise ValueError("no previous version")
    prev = hist.pop()
    attach_source(db, exp, prev["source"], user_id, note=f"rollback to v{prev['version']}")
    exp.validation = {**(exp.validation or {}), "versions": hist}
    return exp


def run_unit(db: Session, exp: StrategyExperiment, user_id: int | None = None) -> dict:
    res = sandbox.run_job("unit", exp.source_code, timeout=240)
    exp.validation = {**(exp.validation or {}), "unit": res}
    db.flush()
    if res["passed"] and normalise(exp.stage) == "DEVELOPING":
        transition(db, exp, "EXPERIMENTAL", user_id, "sandboxed unit tests passed")
    return res


def run_validation(db: Session, exp: StrategyExperiment, bars: pd.DataFrame, costs: dict, user_id: int | None = None) -> dict:
    if normalise(exp.stage) == "EXPERIMENTAL":
        transition(db, exp, "BACKTESTING", user_id, "validation started")
    n_trials = db.scalar(select(func.count(StrategyExperiment.id)).where(StrategyExperiment.source_code.isnot(None))) or 1
    rep = sandbox.run_job("validate", exp.source_code, bars, timeout=900, costs=costs, n_trials=int(n_trials))
    exp.validation = {**(exp.validation or {}), "validation": rep}
    db.flush()
    if rep["validation_passed"] and rep.get("final_test_passed"):
        transition(db, exp, "VALIDATION", user_id, "OOS, walk-forward, Monte Carlo and cost checks passed")
    elif not rep["validation_passed"]:
        failed = [k for k, ok in rep["checks"].items() if not ok]
        _record(db, exp, "REJECTED", user_id, f"validation failed: {failed}")
    else:
        _record(db, exp, "REJECTED", user_id, "passed validation but failed the untouched final test set")
    db.flush()
    return rep


def forward_step(db: Session, exp: StrategyExperiment, symbol: str, bars: pd.DataFrame) -> dict:
    """Shadow (paper) forward test on live broker bars: never sends orders. Records new signals and
    resolves open ones by TP/SL on subsequent bars."""
    v = dict(exp.validation or {})
    fwd = dict(v.get("forward") or {"open": [], "closed": [], "resolved": 0, "wins": 0, "sum_r": 0.0})
    still = []
    for t in fwd["open"]:
        after = bars[bars.index > pd.Timestamp(t["bar_time"])]
        hit = None
        for _, b in after.iterrows():
            d = 1 if t["direction"] == "BUY" else -1
            if (b.low <= t["stop_loss"]) if d > 0 else (b.high >= t["stop_loss"]):
                hit = -1.0
                break
            if (b.high >= t["take_profit"]) if d > 0 else (b.low <= t["take_profit"]):
                hit = t["rr"]
                break
        if hit is None:
            still.append(t)
        else:
            fwd["closed"].append({**t, "r": hit})
            fwd["resolved"] += 1
            fwd["wins"] += int(hit > 0)
            fwd["sum_r"] += hit
    fwd["open"] = still
    out = sandbox.run_job("evaluate", exp.source_code, bars.iloc[-400:], timeout=120)
    sig = out.get("signal")
    if sig and not any(t["bar_time"] == out["bar_time"] and t["symbol"] == symbol for t in fwd["open"] + fwd["closed"][-50:]):
        risk = abs(sig["entry"] - sig["stop_loss"])
        fwd["open"].append({"symbol": symbol, "bar_time": out["bar_time"], "direction": sig["direction"], "entry": sig["entry"],
                            "stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"],
                            "rr": round(abs(sig["take_profit"] - sig["entry"]) / risk, 3) if risk else 0})
    fwd["closed"] = fwd["closed"][-500:]
    fwd["note"] = "Shadow forward test on live DEMO data: hypothetical fills, no orders sent."
    v["forward"] = fwd
    exp.validation = v
    db.flush()
    return {"open": len(fwd["open"]), "resolved": fwd["resolved"]}


def register_library(db: Session) -> dict:
    """Seed the reference experimental strategies (idempotent)."""
    n = 0
    for path in sorted(glob.glob(os.path.join(LIB_DIR, "exp_*.py"))):
        src = open(path, encoding="utf-8").read()
        check = static_validate(src)
        if not check["ok"] or db.scalar(select(StrategyExperiment).where(StrategyExperiment.code == check["code"])):
            continue
        doc = src.split('"""')[1].strip() if '"""' in src else check["name"]
        exp = StrategyExperiment(title=check["name"], hypothesis=" ".join(doc.split()),
                                 proposal={"source": "reference library", "name": check["name"]},
                                 stage="DISCOVERED", evidence={}, duplicate_of=proposal_overlap(doc, db))
        db.add(exp)
        db.flush()
        attach_source(db, exp, src, None, "reference library")
        n += 1
    return {"registered": n}
