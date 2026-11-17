"""Systematic parameter variants and bias report (Parts 27-30 Phase 6, Parts 29.4 and 29.6).

**Variants.** For an original strategy, each numeric parameter is scaled by the given factors (one parameter at a
time). Integers are rounded and kept at 1 or more, positive floats stay positive. Each variant is recorded as an
experiment:
* stage DISCOVERED, with ``proposal.kind = "parameter_variant"``, the base strategy and the full parameter set;
* the original strategy and its production configuration are never touched;
* a variant is only a parameter set for the reviewed original code, so no new code is ever executed;
* duplicates are refused by the experiment's unique code, derived from the parameter set.

**Evaluation** (the learner's experiments job, or a direct call) runs the existing backtester on broker bars:
* the base and the variant, in-sample and out-of-sample;
* plus the bias report. The result is stored in the experiment's evidence.

Promotion follows the existing experiment lifecycle and administrator approval. Nothing here enables anything.

**Bias report:**
* **look-ahead / repaint:** trades closed before a cut point must be identical when the same backtest runs on fewer
  bars (bars after the cut must not change the past);
* **leakage:** no out-of-sample trade enters before the in-sample / out-of-sample cut;
* **survivorship:** symbols removed by the broker (missing) are listed, because evidence gathered only on the
  survivors is biased;
* **multiple testing:** how many variants of this strategy were tried. The best of N variants needs a larger margin,
  reported as a Bonferroni-style required significance level.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import StrategyExperiment, Symbol
from app.strategies import registry

FACTORS = (0.75, 1.25)


def numeric_params(code: str) -> dict:
    cls = registry.get(code)
    return {k: v for k, v in dict(cls.params).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def variant_sets(code: str, factors=FACTORS) -> list[dict]:
    base = dict(registry.get(code).params)
    out, seen = [], set()
    for k, v in numeric_params(code).items():
        for f in factors:
            nv = max(1, int(round(v * f))) if isinstance(v, int) else round(v * f, 6)
            if nv == v or (isinstance(v, float) and v > 0 and nv <= 0):
                continue
            p = {**base, k: nv}
            sig = json.dumps(p, sort_keys=True, default=str)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"changed": {k: [v, nv]}, "params": p})
    return out


def _code(base: str, params: dict) -> str:
    h = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"exp_var_{base}"[:80] + f"_{h}"


def generate(db: Session, code: str, factors=FACTORS) -> dict:
    if code not in registry.all_strategies():
        raise KeyError(code)
    cls = registry.get(code)
    created, existing = [], []
    for v in variant_sets(code, factors):
        ec = _code(code, v["params"])
        if db.scalar(select(StrategyExperiment.id).where(StrategyExperiment.code == ec)):
            existing.append(ec)
            continue
        (k, (old, new)), = v["changed"].items()
        db.add(StrategyExperiment(code=ec, category=cls.category, version=1, title=f"{code}: {k} {old} -> {new}",
                                  hypothesis=f"Changing {k} from {old} to {new} improves out-of-sample results of {code}",
                                  proposal={"kind": "parameter_variant", "base": code, "params": v["params"],
                                            "changed": v["changed"]},
                                  stage="DISCOVERED", evidence={}))
        created.append(ec)
    db.flush()
    return {"base": code, "created": created, "already_existed": existing, "tried_total": n_variants(db, code)}


def n_variants(db: Session, code: str) -> int:
    return int(db.scalar(select(func.count(StrategyExperiment.id))
                         .where(StrategyExperiment.code.like(f"exp_var_{code}"[:80] + "_%"))) or 0)


def _cfg(code: str, params: dict, spec: dict):
    from app.backtesting import engine as bt
    return bt.BacktestConfig([code], spec.get("timeframe", "H1"), params={code: params},
                             costs=bt.CostModel(spec.get("spread_points"), 0.0, 0.0),
                             spec=bt.InstrumentSpec(spec.get("symbol", "SYMBOL"), spec.get("point", 0.00001),
                                                    spec.get("tick_size", 0.00001), spec.get("tick_value", 1.0)))


def bias_report(db: Session | None, df: pd.DataFrame, code: str, params: dict, spec: dict) -> dict:
    from app.backtesting import engine as bt
    cfg = _cfg(code, params, spec)
    cut = int(len(df) * 0.7)
    short = bt.run_backtest(df.iloc[:cut], cfg)["trades"]          # the later bars do not exist at all
    full = bt.run_backtest(df, cfg)["trades"]
    horizon = str(df.index[cut - 2])

    def done(ts):
        return [t for t in ts if t["reason"] != "END" and t["exit_time"] < horizon]
    a, b = done(short), done(full)
    same = [(t["entry_time"], t["exit_time"], t["direction"], round(t["entry"], 8), round(t["exit"], 8)) for t in a] == \
           [(t["entry_time"], t["exit_time"], t["direction"], round(t["entry"], 8), round(t["exit"], 8)) for t in b]
    split = bt.split_oos(df, cfg)
    early = [t for t in split["out_of_sample"]["trades"] if t["entry_time"] < split["cut_time"]]
    missing = []
    trials = 1
    if db is not None:
        missing = [s.broker_symbol for s in db.scalars(select(Symbol).where(Symbol.missing_since.isnot(None)))]
        trials = max(1, n_variants(db, code))
    return {
        "look_ahead": {"passed": same, "compared_trades": len(a),
                       "detail": "trades closed before the cut are identical with and without later bars" if same
                       else "later bars changed earlier trades: look-ahead or repainting"},
        "leakage": {"passed": not early, "cut_time": split["cut_time"],
                    "detail": "no out-of-sample trade entered before the cut" if not early
                    else f"{len(early)} out-of-sample trades entered before the cut"},
        "survivorship": {"passed": not missing, "missing_symbols": missing,
                         "detail": "no symbol has disappeared from the broker" if not missing
                         else "evidence from surviving symbols only is biased; these were removed by the broker"},
        "multiple_testing": {"variants_tried": trials, "required_p": round(0.05 / trials, 5),
                             "detail": f"best of {trials} variants: a 5 % significance level becomes {0.05 / trials:.4g}"},
        "passed": same and not early,
    }


def evaluate(db: Session, exp: StrategyExperiment, df: pd.DataFrame, spec: dict) -> dict:
    """Base vs variant, in-sample and out-of-sample, plus the bias report; stored in the experiment's evidence."""
    from app.backtesting import engine as bt
    prop = exp.proposal or {}
    if prop.get("kind") != "parameter_variant":
        raise ValueError("not a parameter variant")
    base = prop["base"]

    def summary(params):
        r = bt.split_oos(df, _cfg(base, params, spec))
        pick = ("total_trades", "win_rate", "profit_factor", "expected_value", "max_drawdown_pct", "net_profit", "sample_warning")
        return {k: {m: r[k]["metrics"].get(m) for m in pick} for k in ("in_sample", "out_of_sample")}
    out = {"bars": len(df), "symbol": spec.get("symbol"), "timeframe": spec.get("timeframe", "H1"),
           "base": summary(dict(registry.get(base).params)), "variant": summary(prop["params"]),
           "bias": bias_report(db, df, base, prop["params"], spec)}
    exp.evidence = {**(exp.evidence or {}), "variant_evaluation": out}
    return out
