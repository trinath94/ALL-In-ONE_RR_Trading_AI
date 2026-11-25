"""Structured post-trade review (master upgrade, 7.3/7.4), built on the existing failure analysis.

Every closed trade gets answers to the same questions: was the strategy appropriate, the entry correct, the regime
identified correctly, the stop suitable, the exit appropriate, execution acceptable, did the prediction match the
outcome, was it profitable after costs, what mistake occurred, what should improve.

Normal loss vs recurring defect. A loss is not assumed to be a mistake. For a strategy, the losses among its most
recent closed trades are compared with the loss rate expected from its validated evidence (out-of-sample, walk-forward,
paper and live statistics; otherwise its own earlier trades) with a one-sided binomial test. Only a significantly
higher loss rate (p < ``ALPHA``) is reported as a suspected recurring defect; otherwise losses are normal variance.
Rule-based causes with evidence (late entry, slippage, wrong regime, ...) are reported as mistakes of that trade.
"""
from __future__ import annotations

from math import comb

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClosedPosition, Strategy, StrategyPerformance
from app.learning_engine.performance import learnable

ALPHA = 0.05
WINDOW = 30
MIN_EXPECTED_SAMPLE = 30
IMPROVE = {
    "incorrect_regime": "restrict the strategy to its suited regimes (regime filter or a regime profile)",
    "weak_confluence": "require a higher signal grade for this strategy",
    "false_breakout": "add a breakout confirmation (close beyond the level, volume)",
    "poor_entry_timing": "wait for a retest or a better entry; check the entry-validation chase limit",
    "news_volatility": "avoid entries around high-impact news",
    "spread": "lower the maximum spread for this symbol",
    "slippage": "trade at more liquid times or reduce the slippage limit",
    "session_selection": "limit the strategy to its main sessions",
    "model_overconfidence": "recalibrate the probability model",
    "strategy_deterioration": "review the strategy: a recent drop in profit factor",
    "liquidity_sweep_stop": "place the stop beyond the liquidity level (wider stop, smaller size)",
}


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    p = min(max(p, 0.0), 1.0)
    return float(sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _expected_loss_rate(db: Session, code: str, mode: str, earlier: list[float]) -> tuple[float | None, str]:
    sid = db.scalar(select(Strategy.id).where(Strategy.code == code))
    trades = wins = 0
    if sid is not None:
        for r in db.scalars(select(StrategyPerformance).where(StrategyPerformance.strategy_id == sid,
                                                              StrategyPerformance.source.in_(["OOS", "WALKFORWARD",
                                                                                              "PAPER", "LIVE"]))):
            trades, wins = trades + (r.trades or 0), wins + (r.wins or 0)
    if trades >= MIN_EXPECTED_SAMPLE:
        return 1 - wins / trades, f"validated evidence ({trades} trades)"
    if len(earlier) >= MIN_EXPECTED_SAMPLE:
        return sum(1 for x in earlier if x < 0) / len(earlier), f"its earlier {mode} trades ({len(earlier)})"
    return None, "not enough evidence for an expected loss rate"


def loss_significance(db: Session, mode: str, code: str, window: int = WINDOW) -> dict:
    """Is the strategy's recent loss rate higher than its evidence predicts?"""
    rows = db.execute(select(ClosedPosition.profit, ClosedPosition.commission, ClosedPosition.swap)
                      .where(ClosedPosition.mode == mode, ClosedPosition.strategy_codes.contains([code]), learnable())
                      .order_by(ClosedPosition.closed_at)).all()
    nets = [float(p) + float(c or 0) + float(s or 0) for p, c, s in rows]
    recent, earlier = nets[-window:], nets[:-window]
    n, k = len(recent), sum(1 for x in recent if x < 0)
    exp, basis = _expected_loss_rate(db, code, mode, earlier)
    if exp is None or n < 10:
        return {"strategy": code, "verdict": "insufficient_evidence", "recent_trades": n, "recent_losses": k,
                "expected_loss_rate": exp, "basis": basis if exp is None else f"only {n} recent trades"}
    p = binomial_tail(k, n, exp)
    return {"strategy": code, "verdict": "recurring_defect_suspected" if p < ALPHA else "normal_variance",
            "recent_trades": n, "recent_losses": k, "recent_loss_rate": round(k / n, 3),
            "expected_loss_rate": round(exp, 3), "p_value": round(p, 4), "basis": basis,
            "detail": (f"{k}/{n} recent losses vs an expected {exp:.0%} ({basis}): p={p:.3f}"
                       + (" - more losses than the strategy's variance explains" if p < ALPHA
                          else " - within normal variance"))}


def build(db: Session, cp: ClosedPosition, tf, analysis: dict, predicted_p: float | None = None) -> dict:
    """The review of one closed trade from its features and failure analysis."""
    causes = {c["cause"]: c for c in analysis.get("causes", [])}
    strengths = {s["strength"]: s for s in analysis.get("strengths", [])}
    net = float(cp.profit) + float(cp.commission or 0) + float(cp.swap or 0)
    outcome = tf.outcome if tf is not None else ("WIN" if net > 0 else "LOSS" if net < 0 else "BREAKEVEN")
    codes = cp.strategy_codes or []
    mode = cp.mode or "DEMO"
    sig = {c: loss_significance(db, mode, c) for c in codes} if outcome == "LOSS" else {}
    mistakes = [k for k in causes if k != "normal_variance"]
    defect = [c for c, v in sig.items() if v["verdict"] == "recurring_defect_suspected"]
    if outcome != "LOSS":
        verdict = "win" if outcome == "WIN" else "break_even"
    elif defect:
        verdict = "recurring_defect_suspected"
    elif mistakes:
        verdict = "mistake"
    else:
        verdict = "normal_loss"

    def q(passed, detail):
        return {"ok": passed, "detail": detail}
    mfe, mae, r = (getattr(tf, "mfe_r", None), getattr(tf, "mae_r", None), cp.r_multiple)
    slip = getattr(tf, "slippage_points", None)
    return {
        "outcome": outcome, "verdict": verdict, "net_profit": round(net, 2),
        "questions": {
            "strategy_appropriate": q("incorrect_regime" not in causes and "strategy_deterioration" not in causes,
                                      (causes.get("incorrect_regime") or causes.get("strategy_deterioration") or
                                       strengths.get("regime_aligned") or {}).get("evidence", f"strategies {codes or ['manual']}")),
            "entry_correct": q("poor_entry_timing" not in causes and "false_breakout" not in causes,
                               (causes.get("poor_entry_timing") or causes.get("false_breakout") or
                                strengths.get("precise_entry") or {}).get("evidence",
                                                                          f"max favourable {mfe}R, max adverse {mae}R")),
            "regime_identified": q("incorrect_regime" not in causes,
                                   f"regime at entry {getattr(tf, 'regime', None) or 'unknown'}"),
            "stop_suitable": q("liquidity_sweep_stop" not in causes,
                               (causes.get("liquidity_sweep_stop") or {}).get("evidence",
                                                                              f"closed by {cp.close_reason or 'unknown'}")),
            "exit_appropriate": q(outcome != "WIN" or (mfe is None or r is None or mfe <= 0 or r / mfe >= 0.4),
                                  (strengths.get("exit_capture") or {}).get("evidence",
                                                                            f"result {r}R, best {mfe}R, {cp.close_reason}")),
            "execution_acceptable": q("slippage" not in causes and "spread" not in causes,
                                      (causes.get("slippage") or causes.get("spread") or {}).get(
                                          "evidence", f"slippage {slip if slip is not None else 'n/a'} points")),
            "prediction_matched": q(predicted_p is None or (predicted_p >= 0.5) == (outcome == "WIN"),
                                    f"predicted probability {predicted_p if predicted_p is not None else 'n/a'}, "
                                    f"outcome {outcome}"),
            "profitable_after_costs": q(net > 0, f"net {net:.2f} after commission and swap"),
        },
        "mistakes": [{"cause": k, "evidence": causes[k]["evidence"]} for k in mistakes],
        "improvements": [IMPROVE[k] for k in mistakes if k in IMPROVE],
        "loss_significance": list(sig.values()),
        "summary": {"win": "winning trade", "break_even": "break-even trade",
                    "normal_loss": "loss consistent with the strategy's variance; no defect found",
                    "mistake": f"loss with identified mistakes: {', '.join(mistakes)}",
                    "recurring_defect_suspected": f"losses above the expected rate for {', '.join(defect)}"}[verdict],
    }
