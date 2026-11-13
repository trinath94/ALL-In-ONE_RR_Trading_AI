"""Signal generation & opportunity ranking.

Score = sum_k weight_k * component_k   (0..100)

Initial weights follow the specification but are configuration, not truth: they must be
re-validated on out-of-sample data (see app.optimisation.weights). Confidence is never a
made-up percentage: it is the isotonic-calibrated out-of-sample win rate for the score
bucket, and is ``None`` when fewer than ``min_samples`` OOS outcomes exist.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from app.strategies.base import (C_INDICATOR, C_LIQUIDITY, C_MTF, C_PRICE_ACTION, C_SESSION, C_SMC_ICT, C_STRUCTURE,
                                 C_TREND, C_VOLUME, StrategySignal)

DEFAULT_WEIGHTS = {
    C_STRUCTURE: 0.15, C_SMC_ICT: 0.20, C_PRICE_ACTION: 0.15, C_TREND: 0.10, C_INDICATOR: 0.10,
    C_LIQUIDITY: 0.10, C_VOLUME: 0.05, C_MTF: 0.10, C_SESSION: 0.05,
}

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080, "MN1": 43200}


def validate_weights(w: dict[str, float]) -> dict[str, float]:
    missing = set(DEFAULT_WEIGHTS) - set(w)
    if missing:
        raise ValueError(f"missing weights: {missing}")
    if any(v < 0 for v in w.values()):
        raise ValueError("weights must be non-negative")
    total = sum(w[k] for k in DEFAULT_WEIGHTS)
    if total <= 0:
        raise ValueError("weights sum to zero")
    return {k: w[k] / total for k in DEFAULT_WEIGHTS}


@dataclass
class SignalConfig:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    grade_thresholds: dict[str, float] = field(default_factory=lambda: {"A+": 80, "A": 70, "B": 55, "C": 40})
    min_rr: float = 1.5
    conflict_ratio: float = 0.6     # opposite score / main score above this => WAIT
    expiry_bars: int = 3
    regime_mismatch_penalty: float = 0.5
    max_spread_atr: float = 0.25    # spread larger than this share of ATR degrades session component
    min_calibration_samples: int = 30


@dataclass
class Calibrator:
    """Isotonic (pool-adjacent-violators) map from score -> win probability, fit on OOS outcomes."""
    edges: list[float] = field(default_factory=list)
    probs: list[float] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)

    @classmethod
    def fit(cls, scores: list[float], wins: list[bool], n_bins: int = 10) -> "Calibrator":
        if not scores:
            return cls()
        s = np.asarray(scores, float)
        y = np.asarray(wins, float)
        edges = np.unique(np.quantile(s, np.linspace(0, 1, n_bins + 1)))
        if len(edges) < 2:
            return cls([float(s.min()), float(s.max()) + 1e-9], [float(y.mean())], [len(y)])
        idx = np.clip(np.searchsorted(edges, s, side="right") - 1, 0, len(edges) - 2)
        sums = np.bincount(idx, weights=y, minlength=len(edges) - 1)
        cnts = np.bincount(idx, minlength=len(edges) - 1).astype(float)
        blocks = [[sums[i], cnts[i], [i]] for i in range(len(cnts)) if cnts[i] > 0]
        merged: list[list] = []
        for b in blocks:  # PAV: enforce non-decreasing probabilities
            merged.append(b)
            while len(merged) > 1 and merged[-2][0] / merged[-2][1] > merged[-1][0] / merged[-1][1]:
                last = merged.pop()
                merged[-1] = [merged[-1][0] + last[0], merged[-1][1] + last[1], merged[-1][2] + last[2]]
        probs = np.zeros(len(edges) - 1)
        counts = np.zeros(len(edges) - 1, int)
        for sm, ct, ids in merged:
            for i in ids:
                probs[i], counts[i] = sm / ct, int(ct)
        # fill empty bins from neighbours
        for i in range(len(probs)):
            if counts[i] == 0:
                j = max([k for k in range(i) if counts[k] > 0], default=None)
                if j is not None:
                    probs[i], counts[i] = probs[j], 0
        return cls([float(e) for e in edges], [float(p) for p in probs], [int(c) for c in counts])

    @property
    def total(self) -> int:
        return int(sum(self.counts))

    def predict(self, score: float, min_samples: int = 30) -> tuple[float | None, int]:
        if not self.edges or self.total < min_samples:
            return None, self.total
        i = int(np.clip(np.searchsorted(self.edges, score, side="right") - 1, 0, len(self.probs) - 1))
        return round(self.probs[i], 4), self.total


@dataclass
class MarketContext:
    symbol: str
    timeframe: str
    price: float
    bar_time: datetime
    atr: float
    spread: float              # in price units
    regime: str
    regime_tags: list[str]
    htf_bias: int               # +1 / -1 / 0
    sessions: list[str]
    news_status: str = "UNKNOWN"   # CLEAR | RESTRICTED | UNKNOWN
    strategy_regimes: dict[str, list[str]] = field(default_factory=dict)
    strategy_stats: dict[str, dict] = field(default_factory=dict)  # code -> {trades, win_rate, pf, source}


@dataclass
class SignalResult:
    symbol: str
    timeframe: str
    decision: str              # BUY SELL WAIT NO_TRADE
    grade: str                 # A+ A B C REJECTED
    score: float
    direction: str | None
    entry: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    tp3: float | None
    risk_reward: float | None
    strategies: list[dict]
    components: dict[str, float]
    opposing_score: float
    regime: str
    calibrated_confidence: float | None
    confidence_sample: int
    expires_at: datetime
    explanation: str
    invalidation: list[str]
    warnings: list[str]

    def dict(self) -> dict:
        d = asdict(self)
        d["expires_at"] = self.expires_at.isoformat()
        return d


def _component_scores(sigs: list[StrategySignal], ctx: MarketContext, cfg: SignalConfig) -> dict[str, float]:
    comp: dict[str, list[float]] = {}
    for s in sigs:
        strength = s.strength
        suited = ctx.strategy_regimes.get(s.strategy) or []
        if suited and ctx.regime not in suited and not set(suited) & set(ctx.regime_tags):
            strength *= cfg.regime_mismatch_penalty
        stats = ctx.strategy_stats.get(s.strategy)
        if stats and stats.get("trades", 0) >= 30:  # evidence-weighted: poor OOS PF shrinks influence
            pf = stats.get("profit_factor") or 0
            strength *= float(np.clip(pf / 1.3, 0.3, 1.2))
        comp.setdefault(s.component, []).append(min(strength, 1.0))
    out = {}
    for k, vals in comp.items():
        vals = sorted(vals, reverse=True)
        # diminishing returns: strongest + 30% of second + 10% of third
        out[k] = float(min(1.0, vals[0] + 0.3 * (vals[1] if len(vals) > 1 else 0) + 0.1 * (vals[2] if len(vals) > 2 else 0)))
    return out


def build_signal(signals: list[StrategySignal], ctx: MarketContext, cfg: SignalConfig | None = None,
                 calibrator: Calibrator | None = None) -> SignalResult:
    cfg = cfg or SignalConfig()
    w = validate_weights(cfg.weights)
    expiry = ctx.bar_time + timedelta(minutes=TF_MINUTES.get(ctx.timeframe, 60) * cfg.expiry_bars)
    warnings: list[str] = []

    def score_dir(direction: str) -> tuple[float, dict[str, float], list[StrategySignal]]:
        sigs = [s for s in signals if s.direction == direction]
        if not sigs:
            return 0.0, {}, []
        comps = _component_scores(sigs, ctx, cfg)
        want = 1 if direction == "BUY" else -1
        comps[C_MTF] = 1.0 if ctx.htf_bias == want else (0.4 if ctx.htf_bias == 0 else 0.0)
        sess = 1.0 if ctx.sessions else 0.4
        if ctx.atr > 0 and ctx.spread / ctx.atr > cfg.max_spread_atr:
            sess *= 0.3
        if "HIGH_VOLATILITY" in ctx.regime_tags:
            sess *= 0.7
        comps[C_SESSION] = sess
        total = 100 * sum(w[k] * comps.get(k, 0.0) for k in w)
        return total, comps, sigs

    buy = score_dir("BUY")
    sell = score_dir("SELL")
    main, opp, direction = (buy, sell, "BUY") if buy[0] >= sell[0] else (sell, buy, "SELL")
    score, comps, sigs = main

    def result(decision, grade, entry=None, sl=None, tps=(None, None, None), rr=None, expl="", inval=None):
        conf, n = (calibrator.predict(score, cfg.min_calibration_samples) if calibrator else (None, 0))
        if conf is None:
            warnings.append(f"Confidence uncalibrated: {n} out-of-sample outcomes (< {cfg.min_calibration_samples})")
        return SignalResult(ctx.symbol, ctx.timeframe, decision, grade, round(score, 2),
                            direction if decision in ("BUY", "SELL", "WAIT") else None, entry, sl, *tps, rr,
                            [s.dict() for s in sigs], {k: round(v, 3) for k, v in comps.items()},
                            round(opp[0], 2), ctx.regime, conf, n, expiry, expl, inval or [], warnings)

    if not sigs:
        return result("NO_TRADE", "REJECTED", expl="No enabled strategy produced a setup on the last closed bar.")

    if opp[0] > 0 and opp[0] >= cfg.conflict_ratio * score:
        return result("WAIT", "C", expl=f"Conflicting evidence: {direction} {score:.1f} vs opposite {opp[0]:.1f}.")

    # Trade levels: the highest-strength signal provides entry/stop; targets laddered in R.
    lead = max(sigs, key=lambda s: (s.strength, s.risk_reward))
    entry, sl = lead.entry, lead.stop_loss
    risk = abs(entry - sl)
    sgn = 1 if direction == "BUY" else -1
    tp2 = lead.take_profit
    rr = abs(tp2 - entry) / risk if risk > 0 else 0
    tp1 = entry + sgn * min(1.0, rr / 2) * risk
    tp3 = entry + sgn * max(rr * 1.5, 3.0) * risk
    if lead.meta.get("tp2"):
        tp3 = float(lead.meta["tp2"])

    t = cfg.grade_thresholds
    n_comp = sum(1 for k, v in comps.items() if v >= 0.4 and k not in (C_SESSION,))
    if score >= t["A+"] and n_comp >= 3 and rr >= 2:
        grade = "A+"
    elif score >= t["A"] and n_comp >= 2:
        grade = "A"
    elif score >= t["B"]:
        grade = "B"
    elif score >= t["C"]:
        grade = "C"
    else:
        grade = "REJECTED"

    reasons = []
    if rr < cfg.min_rr:
        reasons.append(f"R:R {rr:.2f} below minimum {cfg.min_rr}")
    if ctx.news_status == "RESTRICTED":
        reasons.append("Inside a high-impact news restriction window")
    if ctx.news_status == "UNKNOWN":
        warnings.append("News feed unavailable: news risk is UNKNOWN (not assumed clear)")

    names = ", ".join(sorted({s.strategy for s in sigs}))
    expl = (f"{direction} {ctx.symbol} {ctx.timeframe}: score {score:.1f}/100 ({grade}) from {len(sigs)} strategies "
            f"[{names}]. Regime {ctx.regime}{' ' + str(ctx.regime_tags) if ctx.regime_tags else ''}; "
            f"HTF bias {'+1' if ctx.htf_bias > 0 else ('-1' if ctx.htf_bias < 0 else '0')}. Lead setup: "
            f"{'; '.join(lead.reasons)}.")
    inval = [f"Close {'below' if direction == 'BUY' else 'above'} {sl:.5f} (stop level)",
             f"Signal expires {expiry.isoformat()} if not triggered",
             "Opposite CHOCH on the signal timeframe before entry"]

    if reasons:
        return result("NO_TRADE", "REJECTED", entry, sl, (tp1, tp2, tp3), round(rr, 2), expl + " Rejected: " + "; ".join(reasons), inval)
    decision = direction if grade in ("A+", "A") else ("WAIT" if grade == "B" else "NO_TRADE")
    return result(decision, grade, entry, sl, (tp1, tp2, tp3), round(rr, 2), expl, inval)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
