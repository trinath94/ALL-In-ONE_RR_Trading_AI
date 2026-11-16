"""Adaptive strategy intelligence (RR Trading One Phase 12, specification Part 12).

Built on the existing modules, not beside them:

* evidence: realised trades from ``performance.load_trades`` (one account mode at a time, never mixed) and the
  backtest lab's out-of-sample / walk-forward rows in ``strategy_performance`` (reported separately, never summed
  with realised trades);
* metrics and verdicts: ``performance.compute_metrics`` (INSUFFICIENT_DATA below 30 trades);
* regimes: ``regime_history`` written by the scanner's regime intelligence;
* redundancy: ``intelligence.fingerprint`` (static and behavioural) plus the co-firing observed in live scans;
* deterioration: ``learning_engine.drift`` performance events plus a recent-versus-prior comparison;
* failures: ``failure_reports``; parameter stability: the optimiser's ``optimisation_results``;
* lifecycle: ``learning_engine.models`` (signed artefact, TRAINED -> VALIDATED -> APPROVED -> PRODUCTION, administrator
  approval, rollback).

The original signal engine weights stay the baseline. An adaptive profile only scales the strength of an individual
strategy's setup inside ``signals.engine`` (bounded to [0.5, 1.5]) and only once it is in PRODUCTION; while it is
VALIDATED or APPROVED it runs in shadow, recorded beside the baseline decision without affecting it. Calibrated
confidence is never touched: it stays the out-of-sample win rate of realised outcomes. Nothing here reads AI output.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AnalysisResult, BacktestJob, ClosedPosition, OptimisationResult, Strategy, StrategyPerformance, \
    TradeOrder
from app.db.models_intel import DriftEvent, FailureReport, ModelPrediction, ModelRegistry, RegimeHistory
from app.learning_engine import models as mreg
from app.learning_engine.performance import MIN_TRADES, bootstrap_ci, compute_metrics, load_trades

log = logging.getLogger("rr.adaptive")
UTC = timezone.utc

MODEL_KEY = "adaptive_strategy_weights"
SLICES = ("symbol", "timeframe", "regime", "session")
SHRINK = 30            # prior strength (trades) pulling a slice's mean R towards 0 (= no change from baseline)
MULT_MIN, MULT_MAX = 0.5, 1.5
MIN_PROFILE_TRADES = 150
WF_FOLDS = 3
MIN_EFFECT_R = 0.02     # adaptive weighting must improve mean R per trade by at least this, on unseen trades
EMBARGO = timedelta(days=1)
ANY = "*"


# ------------------------------------------------------------------ evidence ---------------------------------------
def realised(db: Session, mode: str) -> list[dict]:
    """Closed trades of one account mode with an R multiple and at least one attributed strategy, oldest first."""
    return sorted((t for t in load_trades(db, mode) if t["r"] is not None and t["strategies"]),
                  key=lambda t: t["closed_at"])


def _light(trades: list[dict]) -> dict:
    rs = [t["r"] for t in trades if t.get("r") is not None]
    return {"sample_size": len(trades), "verdict": "INSUFFICIENT_DATA",
            "average_r": round(float(np.mean(rs)), 3) if rs else None,
            "win_rate": round(float(np.mean([t["net"] > 0 for t in trades])), 4) if trades else None}


def _metrics(trades: list[dict]) -> dict:
    return compute_metrics(trades) if len(trades) >= MIN_TRADES else _light(trades)


def backtest_evidence(db: Session) -> dict[str, dict[str, dict]]:
    """Out-of-sample and walk-forward results of the backtest lab, per strategy and source (in-sample excluded)."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    q = select(Strategy.code, StrategyPerformance).join(Strategy, Strategy.id == StrategyPerformance.strategy_id) \
        .where(StrategyPerformance.source.in_(["OOS", "WALKFORWARD"]))
    for code, p in db.execute(q):
        d = out[code].setdefault(p.source, {"trades": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0, "runs": 0})
        d["trades"] += p.trades
        d["wins"] += p.wins
        d["gross_profit"] += p.gross_profit
        d["gross_loss"] += p.gross_loss
        d["runs"] += 1
    for srcs in out.values():
        for d in srcs.values():
            d["win_rate"] = round(d["wins"] / d["trades"], 4) if d["trades"] else None
            d["profit_factor"] = round(d["gross_profit"] / d["gross_loss"], 3) if d["gross_loss"] else None
            d["verdict"] = "INSUFFICIENT_DATA" if d["trades"] < MIN_TRADES else "BACKTEST_ONLY"
    return out


# ------------------------------------------------------------------ ranking ----------------------------------------
def ranking(trades: list[dict], backtest: dict[str, dict] | None = None) -> list[dict]:
    """Strategies ranked on the lower 95% bound of realised expectancy: a strategy needs both a good mean and enough
    trades to be confident in it. Strategies below 30 trades are listed, unranked, as INSUFFICIENT_DATA."""
    per: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        for s in t["strategies"]:
            per[s].append(t)
    backtest = backtest or {}
    rows = []
    for code in sorted(set(per) | set(backtest)):
        m = _metrics(per.get(code, []))
        lo = (m.get("expectancy_ci95") or [None])[0]
        rows.append({"strategy": code, "realised": m, "backtest": backtest.get(code, {}), "rank_key": lo})
    ranked = sorted((r for r in rows if r["realised"]["verdict"] != "INSUFFICIENT_DATA" and r["rank_key"] is not None),
                    key=lambda r: -r["rank_key"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    rest = [r for r in rows if "rank" not in r]
    for r in rest:
        r["rank"] = None
    return ranked + sorted(rest, key=lambda r: -r["realised"]["sample_size"])


def best_by_slice(trades: list[dict], top: int = 5) -> dict[str, dict[str, list[dict]]]:
    """For every symbol / timeframe / regime / session: the strategies with a significantly positive edge there
    (lower 95% bound of expectancy above zero), best first. Slices without such evidence report how much they have."""
    out: dict[str, dict[str, list[dict]]] = {}
    for dim in SLICES:
        groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for t in trades:
            k = t.get(dim)
            if k:
                for s in t["strategies"]:
                    groups[k][s].append(t)
        res = {}
        for key, by_s in groups.items():
            cells = []
            for code, ts in by_s.items():
                if len(ts) < MIN_TRADES:
                    continue
                m = compute_metrics(ts)
                cells.append({"strategy": code, "trades": m["sample_size"], "average_r": m["average_r"],
                              "expectancy_ci95": m["expectancy_ci95"], "profit_factor": m["profit_factor"],
                              "verdict": m["verdict"]})
            good = sorted((c for c in cells if c["verdict"] == "POSITIVE_EDGE"), key=lambda c: -(c["expectancy_ci95"][0]))
            res[key] = {"best": good[:top], "evaluated": len(cells),
                        "insufficient": sum(1 for ts in by_s.values() if len(ts) < MIN_TRADES),
                        "negative": [c["strategy"] for c in cells if c["verdict"] == "NEGATIVE_EDGE"]}
        out[dim] = res
    return out


# ------------------------------------------------------------------ deterioration ----------------------------------
def deterioration(db: Session, trades: list[dict], recent: int = MIN_TRADES) -> list[dict]:
    """Strategies whose most recent ``recent`` trades are significantly worse (in R) than the trades before them, plus
    the drift monitor's performance events. A deteriorated strategy is reported, never disabled automatically."""
    per: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        for s in t["strategies"]:
            per[s].append(t["r"])
    out = []
    for code, rs in per.items():
        if len(rs) < 2 * recent:
            continue
        prior, last = np.array(rs[:-recent]), np.array(rs[-recent:])
        rng = np.random.default_rng(17)
        diffs = [rng.choice(last, len(last)).mean() - rng.choice(prior, len(prior)).mean() for _ in range(1000)]
        lo, hi = np.percentile(diffs, [2.5, 97.5])
        if hi < 0:
            out.append({"strategy": code, "source": "realised", "prior_trades": len(prior), "prior_average_r": round(float(prior.mean()), 3),
                        "recent_trades": len(last), "recent_average_r": round(float(last.mean()), 3),
                        "change_ci95": [round(float(lo), 3), round(float(hi), 3)]})
    since = datetime.now(UTC) - timedelta(days=30)
    for ev in db.scalars(select(DriftEvent).where(DriftEvent.scope == "performance", DriftEvent.created_at >= since)
                         .order_by(DriftEvent.id.desc()).limit(200)):
        out.append({"strategy": ev.subject, "source": "drift_monitor", "metric": ev.metric, "value": ev.value,
                    "threshold": ev.threshold, "severity": ev.severity, "at": ev.created_at.isoformat()})
    return out


# ------------------------------------------------------------------ redundancy and conflict ------------------------
def co_firing(db: Session, days: int = 30, min_together: int = 20, limit: int = 20000) -> dict:
    """From the scanner's own records: which strategies fire on the same bar (and in which direction). Frequent
    same-direction co-firing means redundant evidence; frequent opposite directions means conflict."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.scalars(select(AnalysisResult.strategy_outputs).where(AnalysisResult.created_at >= since)
                      .order_by(AnalysisResult.id.desc()).limit(limit)).all()
    fired: Counter = Counter()
    same: Counter = Counter()
    opposite: Counter = Counter()
    for outputs in rows:
        dirs = {o["strategy"]: o.get("direction") for o in outputs or [] if o.get("strategy") and o.get("direction")}
        fired.update(dirs.keys())       # one count per strategy that fired on this bar
        for a, b in combinations(sorted(dirs), 2):
            (same if dirs[a] == dirs[b] else opposite)[(a, b)] += 1
    pairs = []
    for (a, b) in set(same) | set(opposite):
        n = same[(a, b)] + opposite[(a, b)]
        if n < min_together:
            continue
        union = fired[a] + fired[b] - n
        pairs.append({"a": a, "b": b, "together": n, "jaccard": round(n / union, 3) if union else None,
                      "same_direction": same[(a, b)], "opposite_direction": opposite[(a, b)],
                      "kind": ("redundant" if same[(a, b)] >= 0.8 * n and n / max(union, 1) >= 0.5 else
                               "conflicting" if opposite[(a, b)] >= 0.5 * n else "overlapping")})
    return {"analyses": len(rows), "days": days, "strategies_fired": len(fired),
            "redundant": sorted((p for p in pairs if p["kind"] == "redundant"), key=lambda p: -p["together"])[:50],
            "conflicting": sorted((p for p in pairs if p["kind"] == "conflicting"), key=lambda p: -p["together"])[:50],
            "note": None if rows else "no scanner analyses in range"}


def combination_value(trades: list[dict], min_n: int = MIN_TRADES) -> list[dict]:
    """Does a pair of strategies agreeing add predictive value beyond either alone? Compares mean R of trades where
    both contributed with trades where only one of them did; the increment needs a 95% interval above zero."""
    pairs: Counter = Counter()
    for t in trades:
        for a, b in combinations(sorted(set(t["strategies"])), 2):
            pairs[(a, b)] += 1
    out = []
    for (a, b), n in pairs.items():
        if n < min_n:
            continue
        both = np.array([t["r"] for t in trades if a in t["strategies"] and b in t["strategies"]])
        only = np.array([t["r"] for t in trades if (a in t["strategies"]) != (b in t["strategies"])
                         and (a in t["strategies"] or b in t["strategies"])])
        row = {"pair": [a, b], "together": len(both), "together_average_r": round(float(both.mean()), 3),
               "single": len(only)}
        if len(only) < min_n:
            row["verdict"] = "INSUFFICIENT_DATA"
        else:
            rng = np.random.default_rng(23)
            d = [rng.choice(both, len(both)).mean() - rng.choice(only, len(only)).mean() for _ in range(1000)]
            lo, hi = np.percentile(d, [2.5, 97.5])
            row.update(single_average_r=round(float(only.mean()), 3), increment_ci95=[round(float(lo), 3), round(float(hi), 3)],
                       verdict="INDEPENDENT_VALUE" if lo > 0 else ("NO_ADDED_VALUE" if hi < 0 else "INCONCLUSIVE"))
        out.append(row)
    return sorted(out, key=lambda r: (r["verdict"] != "INDEPENDENT_VALUE", -r["together"]))


# ------------------------------------------------------------------ regimes ----------------------------------------
def _regime_means(trades: list[dict]) -> dict[str, dict[str, tuple[float, int]]]:
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        if t.get("regime"):
            for s in t["strategies"]:
                acc[s][t["regime"]].append(t["r"])
    return {s: {r: (float(np.mean(v)), len(v)) for r, v in by.items()} for s, by in acc.items()}


def regime_outlook(db: Session, trades: list[dict], symbol: str, timeframe: str, history: int = 2000) -> dict:
    """Regime probability analysis for one market: how persistent each regime has been (transition matrix of the
    regime history) and, weighting each strategy's realised mean R in each regime by the current regime
    probabilities, which strategies the evidence favours now. Regimes without 30 trades for a strategy count as
    unknown (coverage < 1), never as zero."""
    rows = db.scalars(select(RegimeHistory).where(RegimeHistory.symbol == symbol, RegimeHistory.timeframe == timeframe)
                      .order_by(RegimeHistory.bar_time.desc()).limit(history)).all()[::-1]
    if not rows:
        return {"available": False, "note": "no regime history for this market yet (the scanner records it)"}
    seq = [r.regime for r in rows]
    states = sorted(set(seq))
    T = np.zeros((len(states), len(states)))
    for x, y in zip(seq, seq[1:]):
        T[states.index(x), states.index(y)] += 1
    rs = T.sum(axis=1, keepdims=True)
    P = np.divide(T, rs, out=np.zeros_like(T), where=rs > 0)
    cur = rows[-1]
    probs = {k: float(v) for k, v in (cur.probabilities or {}).items()}
    means = _regime_means(trades)
    favoured = []
    for s, by in means.items():
        num = cov = 0.0
        for reg, p in probs.items():
            if reg in by and by[reg][1] >= MIN_TRADES:
                num += p * by[reg][0]
                cov += p
        if cov > 0:
            favoured.append({"strategy": s, "expected_r_given_regimes": round(num / cov, 3), "coverage": round(cov, 3)})
    return {"available": True, "symbol": symbol, "timeframe": timeframe, "bars": len(seq), "current": cur.regime,
            "probabilities": probs, "confidence": cur.confidence, "stability": cur.stability,
            "transition_warning": cur.transition_warning,
            "persistence": {s: round(float(P[i, i]), 3) for i, s in enumerate(states)},
            "transitions": {"states": states, "matrix": np.round(P, 3).tolist()},
            "favoured": sorted(favoured, key=lambda f: -f["expected_r_given_regimes"])[:15]}


# ------------------------------------------------------------------ failures and parameters -------------------------
def failure_intelligence(db: Session, mode: str, top: int = 5) -> dict:
    """The most frequent recorded causes of losing trades, per strategy (from the failure analysis job)."""
    rows = db.execute(select(FailureReport.causes, ClosedPosition.strategy_codes)
                      .join(ClosedPosition, ClosedPosition.id == FailureReport.closed_position_id)
                      .where(FailureReport.mode == mode, FailureReport.outcome == "LOSS")).all()
    per: dict[str, Counter] = defaultdict(Counter)
    losses: Counter = Counter()
    for causes, codes in rows:
        for s in codes or []:
            losses[s] += 1
            per[s].update({c.get("cause") for c in causes or [] if c.get("cause")})
    return {"losses_analysed": len(rows), "strategies": [
        {"strategy": s, "losses": losses[s], "top_causes": [{"cause": c, "count": n, "share": round(n / losses[s], 3)}
                                                            for c, n in per[s].most_common(top)]}
        for s, _ in losses.most_common(50)]}


def parameter_stability(db: Session) -> list[dict]:
    """Parameter sensitivity from the optimiser's runs: is the in-sample winner still good out of sample, and are its
    neighbours (one parameter changed) also good? STABLE needs all three; otherwise FRAGILE or OVERFIT."""
    out = []
    jobs = db.scalars(select(BacktestJob).where(BacktestJob.kind == "OPTIMISE", BacktestJob.status == "DONE")
                      .order_by(BacktestJob.id.desc()).limit(100)).all()
    for job in jobs:
        rows = db.scalars(select(OptimisationResult).where(OptimisationResult.job_id == job.id)
                          .order_by(OptimisationResult.rank)).all()
        if not rows:
            continue
        best = rows[0]
        ins, oos = best.in_sample or {}, best.out_of_sample or {}
        pf_is, pf_oos = ins.get("profit_factor"), oos.get("profit_factor")
        neighbours = [r for r in rows[1:] if sum(r.params.get(k) != v for k, v in best.params.items()) == 1]
        n_good = sum(1 for r in neighbours if ((r.out_of_sample or {}).get("profit_factor") or 0) > 1)
        trades = oos.get("total_trades") or 0
        if trades < 20:
            verdict = "INSUFFICIENT_DATA"
        elif not pf_oos or pf_oos <= 1:
            verdict = "OVERFIT"
        elif pf_is and pf_oos / pf_is < 0.5 or (neighbours and n_good < len(neighbours) / 2):
            verdict = "FRAGILE"
        else:
            verdict = "STABLE"
        p = job.params or {}
        out.append({"job_id": job.id, "strategy": (p.get("strategies") or [None])[0], "symbol": p.get("symbol"),
                    "timeframe": p.get("timeframe"), "best_params": best.params, "in_sample_pf": pf_is,
                    "out_of_sample_pf": pf_oos, "out_of_sample_trades": trades, "neighbours": len(neighbours),
                    "neighbours_profitable_oos": n_good, "verdict": verdict})
    return out


# ------------------------------------------------------------------ adaptive weight profiles -----------------------
def fit_multipliers(trades: list[dict]) -> dict[str, dict[str, float]]:
    """strategy -> {regime | '*': multiplier}. Shrunk mean R (towards 0 = baseline) mapped to [0.5, 1.5]; a cell is
    only set when it has at least 30 trades, so anything thinner keeps the original engine's behaviour."""
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        for s in t["strategies"]:
            acc[s][ANY].append(t["r"])
            if t.get("regime"):
                acc[s][t["regime"]].append(t["r"])
    out: dict[str, dict[str, float]] = {}
    for s, cells in acc.items():
        for key, rs in cells.items():
            n = len(rs)
            if n < MIN_TRADES:
                continue
            shrunk = n / (n + SHRINK) * float(np.mean(rs))
            out.setdefault(s, {})[key] = round(float(np.clip(1 + shrunk, MULT_MIN, MULT_MAX)), 3)
    return out


def multiplier(mults: dict, strategy: str, regime: str | None) -> float:
    cells = mults.get(strategy) or {}
    return float(cells.get(regime, cells.get(ANY, 1.0)) if regime else cells.get(ANY, 1.0))


def resolve(mults: dict, regime: str | None) -> dict[str, float]:
    """The per-strategy multipliers for the current regime, as ``signals.engine.MarketContext.adaptive`` expects."""
    return {s: multiplier(mults, s, regime) for s in mults}


def _support(mults: dict, t: dict) -> float:
    return float(np.mean([multiplier(mults, s, t.get("regime")) for s in t["strategies"]]))


def _improvement(train: list[dict], test: list[dict]) -> dict:
    """Fit on ``train``, evaluate on unseen ``test``: mean R per trade when each trade is weighted by the profile's
    support for it, minus the unweighted (baseline) mean R. Positive means the profile up-weights the better trades."""
    mults = fit_multipliers(train)
    r = np.array([t["r"] for t in test], float)
    w = np.array([_support(mults, t) for t in test], float)
    base = float(r.mean())
    adapt = float((w * r).sum() / w.sum())
    rng = np.random.default_rng(29)
    boots = []
    for _ in range(1000):
        i = rng.integers(0, len(r), len(r))
        boots.append(float((w[i] * r[i]).sum() / w[i].sum() - r[i].mean()))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"train": len(train), "test": len(test), "baseline_mean_r": round(base, 4), "adaptive_mean_r": round(adapt, 4),
            "improvement_r": round(adapt - base, 4), "improvement_ci95": [round(float(lo), 4), round(float(hi), 4)],
            "cells_changed": sum(1 for c in mults.values() for v in c.values() if v != 1.0),
            "test_start": test[0]["closed_at"].isoformat(), "train_end": train[-1]["closed_at"].isoformat()}


def _split(trades: list[dict], cut: int) -> tuple[list[dict], list[dict]]:
    """Chronological split with an embargo: test trades start more than EMBARGO after the last training trade."""
    train = trades[:cut]
    end = train[-1]["closed_at"] + EMBARGO
    return train, [t for t in trades[cut:] if t["closed_at"] > end]


def walk_forward(trades: list[dict], folds: int = WF_FOLDS) -> dict:
    """Expanding-window comparison of adaptive weighting against the original weights on successive unseen periods."""
    n = len(trades)
    seg = n // (folds + 1)
    out = []
    for k in range(1, folds + 1):
        train, test = _split(trades, seg * k)
        test = test[:seg] if k < folds else test
        if len(test) < MIN_TRADES or len(train) < MIN_TRADES:
            out.append({"fold": k, "skipped": f"train {len(train)} / test {len(test)} trades (min {MIN_TRADES})"})
            continue
        out.append({"fold": k, **_improvement(train, test)})
    scored = [f for f in out if "improvement_r" in f]
    mean = float(np.mean([f["improvement_r"] for f in scored])) if scored else None
    passed = (len(scored) >= 2 and mean is not None and mean >= MIN_EFFECT_R
              and sum(f["improvement_r"] <= 0 for f in scored) <= len(scored) // 3)
    return {"folds": out, "scored": len(scored), "mean_improvement_r": None if mean is None else round(mean, 4),
            "min_effect_r": MIN_EFFECT_R, "passed": bool(passed)}


def _artifact(mults: dict, mode: str) -> bytes:
    return json.dumps({"multipliers": mults, "mode": mode, "bounds": [MULT_MIN, MULT_MAX],
                       "baseline": "signals.engine.DEFAULT_WEIGHTS"}, sort_keys=True, separators=(",", ":")).encode()


def fit_profile(db: Session, mode: str = "DEMO") -> ModelRegistry:
    """Propose an adaptive weight profile from one account mode's realised trades. It becomes VALIDATED only when
    adaptive weighting beats the original weights on held-out trades (95% interval above zero) and across the
    walk-forward folds; it then runs in shadow until an administrator approves and promotes it."""
    trades = realised(db, mode)
    version = (db.scalar(select(func.max(ModelRegistry.version)).where(ModelRegistry.model_key == MODEL_KEY)) or 0) + 1
    reg = ModelRegistry(model_key=MODEL_KEY, version=version, algorithm="shrunk_mean_r_multipliers",
                        target="r_multiple", features=["strategy", "regime"],
                        dataset_ref={"mode": mode, "trades": len(trades), "source": "closed_positions (realised)",
                                     "from": trades[0]["closed_at"].isoformat() if trades else None,
                                     "to": trades[-1]["closed_at"].isoformat() if trades else None},
                        status="TRAINED")
    if len(trades) < MIN_PROFILE_TRADES:
        reg.status = "REJECTED"
        reg.notes = (f"INSUFFICIENT DATA: {len(trades)} {mode} trades with an R multiple and an attributed strategy "
                     f"(min {MIN_PROFILE_TRADES}). The original weights stay in use.")
        db.add(reg)
        db.flush()
        return reg
    train, test = _split(trades, int(len(trades) * 0.7))
    holdout = _improvement(train, test) if len(test) >= MIN_TRADES else None
    wf = walk_forward(trades)
    mults = fit_multipliers(trades)          # the proposal uses all evidence; validation used only past-to-future fits
    blob = _artifact(mults, mode)
    reg.artifact, reg.artifact_sha256, reg.artifact_hmac = blob, hashlib.sha256(blob).hexdigest(), mreg.sign(blob)
    changed = sum(1 for c in mults.values() for v in c.values() if v != 1.0)
    reg.metrics = {"holdout": holdout, "walk_forward": wf, "cells": sum(len(c) for c in mults.values()),
                   "cells_changed": changed, "strategies": len(mults),
                   "largest_increases": sorted(({"strategy": s, "regime": k, "multiplier": v} for s, c in mults.items()
                                                for k, v in c.items() if v > 1), key=lambda x: -x["multiplier"])[:10],
                   "largest_decreases": sorted(({"strategy": s, "regime": k, "multiplier": v} for s, c in mults.items()
                                                for k, v in c.items() if v < 1), key=lambda x: x["multiplier"])[:10]}
    ok_hold = holdout is not None and holdout["improvement_ci95"][0] > 0
    ok = ok_hold and wf["passed"] and changed > 0
    reg.status = "VALIDATED" if ok else "REJECTED"
    reg.validated_at = datetime.now(UTC) if ok else None
    reasons = []
    if not ok_hold:
        reasons.append("no significant improvement on the held-out period" if holdout else "held-out period too small")
    if not wf["passed"]:
        reasons.append("walk-forward folds do not show a consistent improvement")
    if changed == 0:
        reasons.append("no strategy has enough evidence to change its weight")
    reg.notes = ("Adaptive weighting beat the original weights on unseen trades. Runs in shadow; production needs "
                 "administrator approval." if ok else "Rejected: " + "; ".join(reasons) + ". The original weights stay in use.")
    db.add(reg)
    db.flush()
    return reg


def _verified(reg: ModelRegistry | None) -> dict | None:
    if reg is None:
        return None
    try:
        mreg.require_usable(reg)
    except mreg.ModelIntegrityError as exc:
        log.warning("adaptive profile not used: %s", exc)
        return None
    return json.loads(reg.artifact)["multipliers"]


def active_profiles(db: Session) -> dict:
    """{"production": (reg, multipliers) | None, "shadow": (reg, multipliers) | None}. Signatures are verified on
    every call; an unusable profile is treated as absent (the original weights apply)."""
    prod = mreg.production(db, MODEL_KEY)
    shadow = mreg.latest_validated(db, MODEL_KEY)
    pm, sm = _verified(prod), _verified(shadow)
    return {"production": (prod, pm) if pm is not None else None, "shadow": (shadow, sm) if sm is not None else None}


def record_shadow(db: Session, reg: ModelRegistry, baseline, adaptive, applied: dict, live: bool) -> ModelPrediction:
    """Log the adaptive decision beside the baseline one (model_predictions; shadow=True unless the profile is in
    production and therefore produced the decision that was used)."""
    rec = ModelPrediction(model_id=reg.id, symbol=baseline.symbol, timeframe=baseline.timeframe, raw_score=adaptive.score,
                          probability=None, uncertainty=None, shadow=not live,
                          features={"kind": "adaptive_weights", "baseline_decision": baseline.decision,
                                    "baseline_grade": baseline.grade, "baseline_score": baseline.score,
                                    "adaptive_decision": adaptive.decision, "adaptive_grade": adaptive.grade,
                                    "adaptive_score": adaptive.score, "regime": baseline.regime,
                                    "multipliers": {k: v for k, v in applied.items() if v != 1.0}})
    db.add(rec)
    return rec


def shadow_evaluation(db: Session, reg: ModelRegistry) -> dict:
    """How the profile's decisions compared with the original engine's, and what is known of the difference.
    Only trades the baseline actually took have an outcome; a trade the profile alone would have taken has no
    outcome and is counted, never estimated."""
    recs = db.scalars(select(ModelPrediction).where(ModelPrediction.model_id == reg.id)).all()
    trade_dirs = ("BUY", "SELL")
    agree = sum(1 for r in recs if r.features.get("baseline_decision") == r.features.get("adaptive_decision"))
    only_base = [r for r in recs if r.features.get("baseline_decision") in trade_dirs
                 and r.features.get("adaptive_decision") not in trade_dirs]
    only_adapt = sum(1 for r in recs if r.features.get("adaptive_decision") in trade_dirs
                     and r.features.get("baseline_decision") not in trade_dirs)
    ids = [r.signal_id for r in only_base if r.signal_id]
    outcomes = []
    if ids:
        outcomes = [r for (r,) in db.execute(select(ClosedPosition.r_multiple)
                                            .join(TradeOrder, TradeOrder.execution_id == ClosedPosition.execution_id)
                                            .where(TradeOrder.signal_id.in_(ids), ClosedPosition.r_multiple.is_not(None)))]
    rs = np.array(outcomes, float)
    lo, hi = bootstrap_ci(-rs, lambda x: float(x.sum())) if len(rs) >= 5 else (None, None)
    return {"decisions": len(recs), "agreement": round(agree / len(recs), 4) if recs else None,
            "baseline_traded_profile_would_not": len(only_base), "profile_would_trade_baseline_did_not": only_adapt,
            "skipped_trades_with_outcome": int(len(rs)),
            "r_avoided": round(float(-rs.sum()), 3) if len(rs) else None, "r_avoided_ci95": [lo, hi],
            "verdict": "INSUFFICIENT_DATA" if len(rs) < MIN_TRADES else (
                "PROFILE_AVOIDED_LOSSES" if lo is not None and lo > 0 else
                "PROFILE_SKIPPED_WINNERS" if hi is not None and hi < 0 else "INCONCLUSIVE"),
            "note": "Trades only the profile would have taken have no outcome and are not estimated."}
