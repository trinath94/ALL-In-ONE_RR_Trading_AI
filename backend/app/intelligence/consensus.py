"""Multi-specialist consensus.

Specialists reuse the existing strategy implementations (their signals are grouped by family) plus
regime, ML-probability and risk-gate specialists. Claude supervises/explains afterwards.

Weighting:
* If an authorised ``consensus_weights`` model is in PRODUCTION, its out-of-sample-validated weights are
  used (``weights_version = consensus_weights:vN``).
* Otherwise no weights are invented: the engine runs in ``UNVALIDATED_PRIOR`` mode, where correlated
  specialists are grouped into evidence *families* that each count once, and a trade direction needs
  agreement from >= 2 independent families, no strong opposition and a passing risk gate.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_intel import ConsensusDecision, ModelRegistry
from app.strategies.base import StrategySignal

SPECIALISTS = ["SMC", "ICT", "PRICE_ACTION", "LIQUIDITY", "INDICATORS", "QUANT", "VOLUME_ORDER_FLOW", "REGIME", "ML",
               "RISK", "CLAUDE"]
CATEGORY_TO_SPECIALIST = {"smc": "SMC", "ict": "ICT", "sessions": "ICT", "price_action": "PRICE_ACTION",
                          "reversal": "PRICE_ACTION", "fibonacci": "PRICE_ACTION", "liquidity": "LIQUIDITY",
                          "indicators": "INDICATORS", "trend": "INDICATORS", "quantitative": "QUANT",
                          "volume": "VOLUME_ORDER_FLOW"}
COMPONENT_TO_SPECIALIST = {"smc_ict": "SMC", "market_structure": "SMC", "liquidity": "LIQUIDITY", "price_action": "PRICE_ACTION",
                           "trend": "INDICATORS", "indicator": "INDICATORS", "volume": "VOLUME_ORDER_FLOW",
                           "session_volatility": "ICT", "mtf": "INDICATORS"}
# Prior families of correlated evidence (used until weights are validated)
FAMILIES = {"SMC": "structure", "ICT": "structure", "LIQUIDITY": "structure", "PRICE_ACTION": "price",
            "INDICATORS": "technical", "QUANT": "technical", "VOLUME_ORDER_FLOW": "flow", "REGIME": "context",
            "ML": "statistical"}


@dataclass
class SpecialistView:
    specialist: str
    decision: str                 # BUY SELL WAIT NO_TRADE ABSTAIN
    confidence: float
    strategies: list[str] = field(default_factory=list)
    entry_zone: list[float] | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_r: float | None = None
    evidence: list[str] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    model_version: str = "rules-1.0"
    data_quality: str = "ok"


def specialist_from_signals(name: str, sigs: list[StrategySignal], available: bool = True) -> SpecialistView:
    if not available:
        return SpecialistView(name, "ABSTAIN", 0.0, data_quality="unavailable: required data not provided by broker")
    if not sigs:
        return SpecialistView(name, "ABSTAIN", 0.0, evidence=["no setup on the last closed bar"])
    dirs = {s.direction for s in sigs}
    codes = sorted({s.strategy for s in sigs})
    if len(dirs) > 1:
        return SpecialistView(name, "WAIT", 0.0, codes, evidence=["internal conflict: setups in both directions"])
    d = dirs.pop()
    lead = max(sigs, key=lambda s: s.strength)
    conf = float(min(1.0, lead.strength * (1 + 0.1 * (len(sigs) - 1))))
    entries = [s.entry for s in sigs]
    return SpecialistView(name, d, round(conf, 3), codes, [min(entries), max(entries)], lead.stop_loss, lead.take_profit,
                          round(lead.risk_reward, 2), [r for s in sigs for r in s.reasons][:6],
                          [f"close beyond stop {lead.stop_loss:.5f}"])


def regime_specialist(intel: dict | None) -> SpecialistView:
    if not intel:
        return SpecialistView("REGIME", "ABSTAIN", 0.0, data_quality="regime intelligence unavailable")
    r, p = intel["regime"], intel["confidence"]
    ver = intel.get("model_version", "regime")
    if r in ("STRONG_BULL", "WEAK_BULL"):
        return SpecialistView("REGIME", "BUY", p, evidence=[f"{r} p={p}"], model_version=ver)
    if r in ("STRONG_BEAR", "WEAK_BEAR"):
        return SpecialistView("REGIME", "SELL", p, evidence=[f"{r} p={p}"], model_version=ver)
    if r in ("UNCERTAIN", "NEWS_DRIVEN"):
        return SpecialistView("REGIME", "NO_TRADE", p, evidence=[f"{r}: {intel.get('transition_warning') or ''}"],
                              model_version=ver)
    return SpecialistView("REGIME", "WAIT", p, evidence=[f"{r}: no directional regime"], model_version=ver)


def ml_specialist(prob: dict | None, direction: str | None) -> SpecialistView:
    if not prob or not prob.get("available"):
        return SpecialistView("ML", "ABSTAIN", 0.0, data_quality=(prob or {}).get("status", "no model"))
    if prob.get("shadow"):
        return SpecialistView("ML", "ABSTAIN", 0.0, evidence=[f"shadow model {prob['model']} (not approved)"],
                              model_version=prob["model"], data_quality="shadow")
    er, p = prob.get("expected_r_net"), prob.get("tp_before_sl_probability")
    if prob.get("low_data_confidence"):
        return SpecialistView("ML", "WAIT", 0.0, evidence=["LOW DATA CONFIDENCE"], model_version=prob["model"])
    if er is not None and er > 0.1 and direction:
        return SpecialistView("ML", direction, round(p, 3), evidence=[f"P(TP first)={p}, E[R net]={er}"], expected_r=er,
                              model_version=prob["model"])
    return SpecialistView("ML", "NO_TRADE", round(1 - (p or 0), 3), evidence=[f"E[R net]={er} not positive enough"],
                          expected_r=er, model_version=prob["model"])


def risk_specialist(gates: list[str]) -> SpecialistView:
    if gates:
        return SpecialistView("RISK", "NO_TRADE", 1.0, evidence=gates)
    return SpecialistView("RISK", "PASS", 1.0, evidence=["pre-trade gates clear (final authority: Risk Engine)"])


def validated_weights(db: Session) -> tuple[dict[str, float] | None, str]:
    reg = db.scalar(select(ModelRegistry).where(ModelRegistry.model_key == "consensus_weights",
                                                ModelRegistry.status == "PRODUCTION"))
    if reg is None:
        return None, "UNVALIDATED_PRIOR"
    return reg.metrics.get("weights"), f"consensus_weights:v{reg.version}"


def combine(views: list[SpecialistView], weights: dict[str, float] | None, weights_version: str) -> dict:
    risk = next((v for v in views if v.specialist == "RISK"), None)
    voting = [v for v in views if v.specialist not in ("RISK", "CLAUDE") and v.decision in ("BUY", "SELL", "NO_TRADE", "WAIT")]
    conflicts = []
    if risk and risk.decision == "NO_TRADE":
        return {"decision": "NO_TRADE", "agreement": 0.0, "conflicts": risk.evidence, "direction": None,
                "explanation": "Risk gate blocked: " + "; ".join(risk.evidence), "weights_version": weights_version}
    if weights:
        score = defaultdict(float)
        for v in voting:
            w = weights.get(v.specialist, 0.0)
            if v.decision in ("BUY", "SELL"):
                score[v.decision] += w * v.confidence
            elif v.decision == "NO_TRADE":
                score["NO_TRADE"] += w * v.confidence
        total = sum(score.values()) or 1.0
        best = max(score, key=score.get) if score else "NO_TRADE"
        agreement = score.get(best, 0) / total
        if best in ("BUY", "SELL") and score.get("SELL" if best == "BUY" else "BUY", 0) > 0.3 * score[best]:
            conflicts.append("material opposing weighted evidence")
            best = "WAIT"
        decision = best if agreement >= 0.6 else ("WAIT" if best in ("BUY", "SELL") else "NO_TRADE")
        return {"decision": decision, "agreement": round(agreement, 3), "conflicts": conflicts,
                "direction": best if best in ("BUY", "SELL") else None, "weights_version": weights_version,
                "explanation": f"validated weights: {dict(score)}"}
    # Prior mode: count independent families once each
    fam_votes: dict[str, dict[str, float]] = defaultdict(dict)
    for v in voting:
        fam = FAMILIES.get(v.specialist, v.specialist)
        prev = fam_votes[fam].get(v.decision, 0.0)
        fam_votes[fam][v.decision] = max(prev, v.confidence)
    fam_dir = {}
    for fam, votes in fam_votes.items():
        dirs = {d for d in votes if d in ("BUY", "SELL")}
        if len(dirs) == 2:
            conflicts.append(f"{fam} family split BUY/SELL")
            fam_dir[fam] = "CONFLICT"
        elif dirs:
            fam_dir[fam] = dirs.pop()
        elif "NO_TRADE" in votes:
            fam_dir[fam] = "NO_TRADE"
    buys = [f for f, d in fam_dir.items() if d == "BUY"]
    sells = [f for f, d in fam_dir.items() if d == "SELL"]
    vetoes = [f for f, d in fam_dir.items() if d == "NO_TRADE"]
    n_fam = max(len(fam_dir), 1)
    if buys and sells:
        conflicts.append(f"families disagree: BUY {buys} vs SELL {sells}")
    lead, other = (buys, sells) if len(buys) >= len(sells) else (sells, buys)
    direction = "BUY" if lead is buys else "SELL"
    agreement = len(lead) / n_fam
    if len(lead) >= 2 and not other and not vetoes:
        decision = direction
    elif lead and (other or vetoes):
        decision = "WAIT"
    elif len(lead) == 1:
        decision = "WAIT"
        conflicts.append("insufficient independent evidence (1 family)")
    else:
        decision, direction = "NO_TRADE", None
    return {"decision": decision, "agreement": round(agreement, 3), "conflicts": conflicts,
            "direction": direction if decision in ("BUY", "SELL", "WAIT") and lead else None,
            "weights_version": weights_version,
            "explanation": f"prior mode (weights not yet validated): families {fam_dir}; vetoes {vetoes}"}


def run(db: Session, signals: list[StrategySignal], categories: dict[str, str], regime_intel: dict | None,
        prob: dict | None, gates: list[str], engine_direction: str | None, caps_available: dict[str, bool] | None = None,
        record: dict | None = None) -> dict:
    groups: dict[str, list[StrategySignal]] = defaultdict(list)
    for s in signals:
        cat = categories.get(s.strategy, "")
        name = CATEGORY_TO_SPECIALIST.get(cat) or COMPONENT_TO_SPECIALIST.get(s.component, "INDICATORS")
        groups[name].append(s)
    caps_available = caps_available or {}
    views = [specialist_from_signals(n, groups.get(n, []), caps_available.get(n, True))
             for n in ("SMC", "ICT", "PRICE_ACTION", "LIQUIDITY", "INDICATORS", "QUANT", "VOLUME_ORDER_FLOW")]
    views += [regime_specialist(regime_intel), ml_specialist(prob, engine_direction), risk_specialist(gates)]
    weights, version = validated_weights(db)
    res = combine(views, weights, version)
    res["specialists"] = [asdict(v) for v in views]
    if record is not None:
        rec = ConsensusDecision(analysis_id=record.get("analysis_id"), signal_id=record.get("signal_id"),
                                symbol=record["symbol"], timeframe=record["timeframe"], decision=res["decision"],
                                agreement=res["agreement"], specialists=res["specialists"], conflicts=res["conflicts"],
                                weights_version=version, explanation=res["explanation"])
        db.add(rec)
        res["record"] = rec          # ORM object for the caller to link; not JSON-serialisable
    return res


def vote_matrix(db: Session, min_rows: int = 50) -> dict:
    """Empirical correlation of specialist votes (+1 BUY, -1 SELL, 0 otherwise) — shows which specialists
    are effectively the same evidence."""
    rows = db.scalars(select(ConsensusDecision).order_by(ConsensusDecision.id.desc()).limit(5000)).all()
    if len(rows) < min_rows:
        return {"available": False, "rows": len(rows), "note": f"need >= {min_rows} consensus records"}
    names = SPECIALISTS[:-2]
    M = np.array([[{"BUY": 1, "SELL": -1}.get(next((s["decision"] for s in r.specialists if s["specialist"] == n), ""), 0)
                   for n in names] for r in rows], float)
    keep = M.std(axis=0) > 0
    C = np.corrcoef(M[:, keep].T) if keep.sum() > 1 else np.eye(1)
    ev = np.clip(np.linalg.eigvalsh(C), 0, None)
    eff = float(ev.sum() ** 2 / (ev ** 2).sum()) if ev.sum() > 0 else 0.0
    return {"available": True, "rows": len(rows), "specialists": [n for n, k in zip(names, keep) if k],
            "matrix": np.round(C, 3).tolist(), "effective_independent_specialists": round(eff, 2)}
