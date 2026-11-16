"""Probability and signal fusion (RR Trading One Phase 13, specification Part 13).

One explainable decision per analysis, combining what the scanner already computes:

* the original strategy outputs and the original signal engine's decision (``signals.engine``: the benchmark);
* market regime (regime intelligence), multi-timeframe alignment, independent evidence families and the
  correlation-adjusted consensus (``intelligence.consensus``);
* historical performance (realised, per account mode, from the learner's performance snapshots);
* calibrated probabilities: the probability model in PRODUCTION (``intelligence.probability``) and the engine's own
  out-of-sample calibration of its score; a raw score is never presented as a probability;
* news risk, spread, market anomalies and portfolio exposure.

Rules
* The fused decision is never more aggressive than the original engine: BUY/SELL can stay, or become WAIT or NO_TRADE;
  it is never upgraded and never reversed. WAIT and NO_TRADE are never executable.
* Hard blocks (news restriction window, unresolved critical anomaly) give NO_TRADE. Disagreement from the consensus or
  from a calibrated probability model gives WAIT. Everything else is reported for the explanation only, so evidence is
  not counted twice (the engine's score already includes multi-timeframe, session and spread effects; the Risk Engine
  remains the authority on exposure at execution time).
* ``evidence``: CALIBRATED when an out-of-sample probability with enough samples exists; LOW_DATA_CONFIDENCE when some
  exists but too little; INSUFFICIENT_DATA when none. The decision is not upgraded by evidence, only explained.
* ``RR_FUSION_MODE``: ``advisory`` (default) keeps the original decision and records the fused one beside it, so the
  two can be compared; ``enforce`` uses the fused decision for signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

TRADE = ("BUY", "SELL")
MIN_CALIBRATION = 30
SUPPORTS, OPPOSES, NEUTRAL, BLOCKS, UNAVAILABLE, CAUTION = "SUPPORTS", "OPPOSES", "NEUTRAL", "BLOCKS", "UNAVAILABLE", "CAUTION"
BULL = ("STRONG_BULL", "WEAK_BULL")
BEAR = ("STRONG_BEAR", "WEAK_BEAR")


@dataclass
class Factor:
    factor: str
    status: str
    detail: str
    source: str
    effect: str = "explains"       # explains | downgrades_to_WAIT | blocks

    def dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Fusion:
    decision: str
    direction: str | None
    executable: bool
    benchmark: dict
    evidence: str
    probability: dict | None
    score: dict
    factors: list[Factor] = field(default_factory=list)
    explanation: str = ""
    mode: str = "advisory"

    def dict(self) -> dict:
        d = dict(self.__dict__)
        d["factors"] = [f.dict() for f in self.factors]
        return d


def _against(direction: str | None, other: str | None) -> bool:
    return bool(direction and other in TRADE and other != direction)


def probability_evidence(engine, prob: dict | None) -> tuple[str, dict | None]:
    """(evidence status, the probability to show). Only calibrated out-of-sample estimates are shown, with their source
    and sample; a shadow (unapproved) model is never used."""
    if prob and prob.get("available") and not prob.get("shadow") and prob.get("tp_before_sl_probability") is not None:
        p = {"value": prob["tp_before_sl_probability"], "kind": "P(take-profit before stop-loss)",
             "source": prob.get("model"), "samples": prob.get("calibration_bin_samples"),
             "uncertainty_95": prob.get("uncertainty_95"), "expected_r_net": prob.get("expected_r_net")}
        return ("LOW_DATA_CONFIDENCE" if prob.get("low_data_confidence") else "CALIBRATED"), p
    if engine.calibrated_confidence is not None:
        return "CALIBRATED", {"value": engine.calibrated_confidence, "kind": "win rate of signals with this score",
                              "source": "signal engine out-of-sample calibration", "samples": engine.confidence_sample}
    if engine.confidence_sample > 0:
        return "LOW_DATA_CONFIDENCE", None
    return "INSUFFICIENT_DATA", None


def historical(db: Session, mode: str, strategies: list[str]) -> dict:
    """Realised performance of the contributing strategies in the active account mode (learner snapshots)."""
    from app.learning_engine import performance as perf
    rows = {r.key: r for r in perf.latest(db, mode, "strategy")} if strategies else {}
    out = {}
    for code in strategies:
        r = rows.get(code)
        out[code] = ({"trades": r.sample_size, "verdict": r.metrics.get("verdict"), "average_r": r.metrics.get("average_r")}
                     if r else {"trades": 0, "verdict": "NO_DATA"})
    return out


def exposure(db: Session, symbol: str, direction: str | None) -> dict:
    """Open positions that the candidate would add to or offset (same symbol, or a correlated symbol |rho| >= 0.7).
    Informational: the Risk Engine enforces the exposure limits when an order is submitted."""
    from app.db.models import OpenPosition
    from app.intelligence import portfolio
    from app.services import trading as tr
    acct = tr.active_account(db)
    if acct is None or direction not in TRADE:
        return {"available": acct is not None, "related": []}
    corr = portfolio.cached_corr()
    sgn = 1 if direction == "BUY" else -1
    related = []
    for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == acct.id)):
        rho = 1.0 if p.symbol == symbol else corr.get((symbol, p.symbol), corr.get((p.symbol, symbol)))
        if rho is None or abs(rho) < 0.7:
            continue
        same_way = sgn * (1 if p.direction == "BUY" else -1) * rho > 0
        related.append({"symbol": p.symbol, "direction": p.direction, "correlation": round(float(rho), 3),
                        "effect": "adds to it" if same_way else "offsets it"})
    return {"available": True, "related": related}


def fuse(engine, intel: dict, *, news: str, spread: float, atr: float, htf_bias: int, max_spread_atr: float,
         history: dict | None = None, exposure_info: dict | None = None, weighting: str = "original weights",
         mode: str = "advisory") -> Fusion:
    d = engine.direction if engine.decision in TRADE + ("WAIT",) else None
    f: list[Factor] = []
    n_strat = len({s["strategy"] for s in engine.strategies})
    f.append(Factor("strategies", SUPPORTS if engine.decision in TRADE else NEUTRAL,
                    f"{n_strat} strateg{'y' if n_strat == 1 else 'ies'} for {d or 'no direction'}; original engine "
                    f"{engine.decision} grade {engine.grade}, confluence score {engine.score}/100 ({weighting})",
                    "signals.engine (benchmark)"))
    # regime
    reg = intel.get("regime") or {}
    if not reg or reg.get("error"):
        f.append(Factor("regime", UNAVAILABLE, reg.get("error") or "regime intelligence disabled", "intelligence.regime"))
    else:
        r = reg["regime"]
        st = (SUPPORTS if (d == "BUY" and r in BULL) or (d == "SELL" and r in BEAR) else
              OPPOSES if (d == "BUY" and r in BEAR) or (d == "SELL" and r in BULL) else
              CAUTION if r in ("UNCERTAIN", "NEWS_DRIVEN") else NEUTRAL)
        f.append(Factor("regime", st, f"{r} (confidence {reg.get('confidence')}, stability {reg.get('stability')})"
                        + (f"; {reg['transition_warning']}" if reg.get("transition_warning") else ""), "intelligence.regime"))
    # multi-timeframe
    want = 1 if d == "BUY" else -1 if d == "SELL" else 0
    f.append(Factor("multi_timeframe", NEUTRAL if not want or htf_bias == 0 else SUPPORTS if htf_bias == want else OPPOSES,
                    f"higher-timeframe bias {'+1' if htf_bias > 0 else '-1' if htf_bias < 0 else '0'} "
                    "(already part of the engine score)", "signals.engine"))
    # consensus: independent evidence families, correlation-adjusted
    cons = intel.get("consensus")
    downgrade, blocks = [], []
    if not cons:
        f.append(Factor("consensus", UNAVAILABLE, "not evaluated (the engine produced no direction)", "intelligence.consensus"))
    else:
        st = SUPPORTS if cons["decision"] == d else OPPOSES if (_against(d, cons["decision"]) or cons["decision"] == "NO_TRADE") else CAUTION
        fac = Factor("consensus", st, f"{cons['decision']} with agreement {cons['agreement']} ({cons['weights_version']})"
                     + (f"; conflicts: {'; '.join(cons['conflicts'][:3])}" if cons.get("conflicts") else ""),
                     "intelligence.consensus")
        if engine.decision in TRADE and st == OPPOSES:
            fac.effect = "downgrades_to_WAIT"
            downgrade.append(f"consensus {cons['decision']}")
        f.append(fac)
    # probability
    prob = intel.get("probability")
    evidence, shown = probability_evidence(engine, prob)
    if shown and shown["source"] != "signal engine out-of-sample calibration":
        er = shown.get("expected_r_net")
        st = SUPPORTS if er is not None and er > 0.1 else OPPOSES if er is not None and er <= 0 else CAUTION
        fac = Factor("probability", st, f"{shown['kind']} {shown['value']} ({shown['source']}, {shown['samples']} samples in "
                     f"its calibration bin); expected R after costs {er}", "intelligence.probability")
        if engine.decision in TRADE and st == OPPOSES and evidence == "CALIBRATED":
            fac.effect = "downgrades_to_WAIT"
            downgrade.append("calibrated probability model expects no positive R after costs")
        f.append(fac)
    else:
        note = (prob or {}).get("note") or (prob or {}).get("status") or "no probability model in production"
        if prob and prob.get("shadow"):
            note = f"model {prob.get('model')} is not approved (shadow): not used"
        f.append(Factor("probability", UNAVAILABLE, note, "intelligence.probability"))
    f.append(Factor("calibration", SUPPORTS if engine.calibrated_confidence is not None else UNAVAILABLE,
                    (f"signals with this score won {engine.calibrated_confidence} of the time (n={engine.confidence_sample})"
                     if engine.calibrated_confidence is not None else
                     f"{engine.confidence_sample} out-of-sample outcomes (< {MIN_CALIBRATION}): no calibrated confidence"),
                    "signals.engine.Calibrator"))
    # historical performance (realised, this account mode)
    if history:
        known = {k: v for k, v in history.items() if v["trades"]}
        pos = [k for k, v in known.items() if v["verdict"] == "POSITIVE_EDGE"]
        neg = [k for k, v in known.items() if v["verdict"] == "NEGATIVE_EDGE"]
        st = OPPOSES if neg and not pos else SUPPORTS if pos and not neg else NEUTRAL if known else UNAVAILABLE
        f.append(Factor("historical_performance", st,
                        ("; ".join(f"{k}: {v['trades']} trades, {v['verdict']}" for k, v in known.items()) if known else
                         "no realised trades for these strategies in this account mode"), "learning_engine.performance"))
    # news, spread, anomalies, exposure
    if news == "RESTRICTED":
        f.append(Factor("news", BLOCKS, "inside a high-impact news restriction window", "news.service", "blocks"))
        blocks.append("news restriction window")
    else:
        f.append(Factor("news", NEUTRAL if news == "CLEAR" else CAUTION,
                        "no high-impact event in the window" if news == "CLEAR" else "news feed unavailable: risk unknown, not assumed clear",
                        "news.service"))
    ratio = spread / atr if atr > 0 else None
    f.append(Factor("spread", UNAVAILABLE if ratio is None else CAUTION if ratio > max_spread_atr else NEUTRAL,
                    "no ATR" if ratio is None else f"spread {ratio:.0%} of ATR (limit {max_spread_atr:.0%}; already part of the engine score)",
                    "signals.engine"))
    an = intel.get("anomalies") or {}
    if an.get("blocking"):
        f.append(Factor("anomalies", BLOCKS, f"unresolved critical anomaly: {', '.join(an['blocking'])}", "intelligence.anomaly", "blocks"))
        blocks.append("critical market anomaly")
    else:
        f.append(Factor("anomalies", UNAVAILABLE if an.get("error") else NEUTRAL,
                        an.get("error") or f"{len(an.get('found') or [])} non-blocking finding(s)", "intelligence.anomaly"))
    if exposure_info is not None:
        rel = exposure_info.get("related") or []
        f.append(Factor("portfolio_exposure", UNAVAILABLE if not exposure_info.get("available") else CAUTION if rel else NEUTRAL,
                        ("; ".join(f"{x['effect']}: {x['direction']} {x['symbol']} (rho {x['correlation']})" for x in rel)
                         if rel else "no related open position") + " — limits are enforced by the Risk Engine at execution",
                        "intelligence.portfolio"))
    # decision: never more aggressive than the benchmark
    if engine.decision == "NO_TRADE":
        decision = "NO_TRADE"
    elif blocks:
        decision = "NO_TRADE"
    elif engine.decision in TRADE and downgrade:
        decision = "WAIT"
    else:
        decision = engine.decision
    direction = d if decision in TRADE + ("WAIT",) else None
    why = ("blocked: " + "; ".join(blocks)) if blocks and engine.decision != "NO_TRADE" else (
        "downgraded to WAIT: " + "; ".join(downgrade)) if decision == "WAIT" and engine.decision in TRADE else (
        "same as the original engine")
    label = {"CALIBRATED": "calibrated out-of-sample probability", "LOW_DATA_CONFIDENCE": "LOW DATA CONFIDENCE",
             "INSUFFICIENT_DATA": "INSUFFICIENT DATA"}[evidence]
    expl = (f"{decision}{' ' + direction if decision == 'WAIT' and direction else ''} ({why}). Evidence: {label}"
            + (f" — {shown['kind']} {shown['value']} from {shown['source']}" if shown else "") + ". "
            + "; ".join(f"{x.factor} {x.status.lower()}" for x in f if x.status != NEUTRAL) + ".")
    return Fusion(decision=decision, direction=direction, executable=decision in TRADE,
                  benchmark={"decision": engine.decision, "grade": engine.grade, "direction": engine.direction},
                  evidence=evidence, probability=shown,
                  score={"value": engine.score, "label": "confluence score 0-100 (not a probability)"},
                  factors=f, explanation=expl, mode=mode)


def benchmark_report(db: Session, days: int = 30) -> dict:
    """Fused decisions against the original engine's: how often they differ, and, where the original engine's
    signal was traded and closed, what the fused decision would have avoided. Trades only the fused decision would
    have changed but that were never taken have no outcome and are counted, never estimated."""
    from datetime import UTC, datetime, timedelta

    import numpy as np

    from app.db.models import AnalysisResult, ClosedPosition, TradeOrder, TradingSignal
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(AnalysisResult.id, AnalysisResult.score).where(AnalysisResult.created_at >= since)).all()
    total = differ = 0
    kinds: dict[str, int] = {}
    held_back: list[int] = []
    evidence: dict[str, int] = {}
    for aid, score in rows:
        fu = (score or {}).get("fusion")
        if not fu:
            continue
        total += 1
        evidence[fu["evidence"]] = evidence.get(fu["evidence"], 0) + 1
        b = fu["benchmark"]["decision"]
        if b != fu["decision"]:
            differ += 1
            k = f"{b} -> {fu['decision']}"
            kinds[k] = kinds.get(k, 0) + 1
            if b in TRADE:
                held_back.append(aid)
    rs = []
    if held_back:
        rs = [r for (r,) in db.execute(select(ClosedPosition.r_multiple)
                                       .join(TradeOrder, TradeOrder.execution_id == ClosedPosition.execution_id)
                                       .join(TradingSignal, TradingSignal.id == TradeOrder.signal_id)
                                       .where(TradingSignal.analysis_id.in_(held_back), ClosedPosition.r_multiple.is_not(None)))]
    from app.learning_engine.performance import bootstrap_ci
    arr = np.array(rs, float)
    lo, hi = bootstrap_ci(-arr, lambda x: float(x.sum())) if len(arr) >= 5 else (None, None)
    return {"days": days, "fused_analyses": total, "differ": differ, "changes": kinds, "evidence": evidence,
            "held_back_trades_with_outcome": int(len(arr)),
            "r_avoided": round(float(-arr.sum()), 3) if len(arr) else None, "r_avoided_ci95": [lo, hi],
            "verdict": "INSUFFICIENT_DATA" if len(arr) < MIN_CALIBRATION else (
                "FUSION_AVOIDED_LOSSES" if lo is not None and lo > 0 else
                "FUSION_HELD_BACK_WINNERS" if hi is not None and hi < 0 else "INCONCLUSIVE"),
            "note": "Only original-engine trades that were actually taken have outcomes."}
