"""Failure & success intelligence on completed trades, plus missed-opportunity tracking.

Rules are evidence-based and each cause carries the measured evidence that triggered it. Missed
opportunities (rejected/expired signals) are evaluated on *subsequent* bars, stored separately and
labelled hypothetical — they never enter performance statistics and never prove a rejection was wrong.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClosedPosition, TradeOrder, TradingSignal, Symbol
from app.db.models_intel import AIRecommendation, FailureReport, MissedOpportunity, ModelPrediction, TradeFeatures
from app.learning_engine.memory import remember
from app.learning_engine.performance import learnable
from app.strategies import registry

UTC = timezone.utc
BREAKOUT_HINTS = ("breakout", "bos", "donchian", "orb", "first15", "first30", "initial_balance", "displacement")
TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def _suited(codes: list[str]) -> set[str]:
    out: set[str] = set()
    reg = registry.all_strategies()
    for c in codes:
        cls = reg.get(c)
        if cls is None:
            continue
        if not cls.regimes:
            return set()          # at least one strategy suits all regimes
        out |= set(cls.regimes)
    return out


def analyse_trade(cp: ClosedPosition, tf: TradeFeatures, post_bars: pd.DataFrame | None = None,
                  predicted_p: float | None = None, recent_strategy_stats: dict | None = None) -> dict:
    causes: list[dict] = []
    strengths: list[dict] = []
    codes = cp.strategy_codes or []
    suited = _suited(codes)
    add = lambda lst, name, ev, conf: lst.append({"cause" if lst is causes else "strength": name, "evidence": ev,  # noqa: E731
                                                  "confidence": conf})
    loss = tf.outcome == "LOSS"
    if loss:
        if tf.regime and suited and tf.regime not in suited:
            add(causes, "incorrect_regime", f"entered in {tf.regime}; strategies suited to {sorted(suited)}", 0.7)
        if tf.grade and tf.grade not in ("A+", "A"):
            add(causes, "weak_confluence", f"signal grade {tf.grade}, score {tf.confluence_score}", 0.6)
        if any(h in c for c in codes for h in BREAKOUT_HINTS) and tf.mfe_r is not None and tf.mfe_r < 0.5:
            add(causes, "false_breakout", f"breakout entry reached only {tf.mfe_r}R before stop", 0.6)
        if tf.mfe_r is not None and tf.mfe_r < 0.2:
            add(causes, "poor_entry_timing", f"max favourable excursion {tf.mfe_r}R — trade never moved in favour", 0.5)
        if tf.news_status in ("RESTRICTED", "UNKNOWN"):
            add(causes, "news_volatility", f"news status at entry: {tf.news_status}", 0.5 if tf.news_status == "UNKNOWN" else 0.7)
        if tf.spread_points is not None and tf.spread_points > 20:
            add(causes, "spread", f"spread {tf.spread_points} points at analysis time", 0.5)
        if tf.slippage_points is not None and tf.slippage_points > 10:
            add(causes, "slippage", f"entry slippage {tf.slippage_points} points", 0.6)
        if tf.session == "OFF_SESSION":
            add(causes, "session_selection", "entered outside main sessions", 0.4)
        if predicted_p is not None and predicted_p >= 0.65:
            add(causes, "model_overconfidence", f"model probability {predicted_p:.2f} on a losing trade", 0.4)
        if recent_strategy_stats:
            for code, st in recent_strategy_stats.items():
                if st.get("deteriorating"):
                    add(causes, "strategy_deterioration", f"{code}: {st['detail']}", 0.6)
        if post_bars is not None and len(post_bars) and cp.stop_loss and cp.close_reason == "STOP_LOSS":
            sgn = 1 if cp.direction == "BUY" else -1
            risk = abs(float(cp.open_price) - float(cp.stop_loss))
            target = float(cp.open_price) + sgn * 2 * risk
            reached = (post_bars["high"].max() >= target) if sgn > 0 else (post_bars["low"].min() <= target)
            if reached:
                add(causes, "liquidity_sweep_stop", "stop hit, then price reached +2R within the following bars "
                                                    "(stop likely inside liquidity)", 0.6)
        if not causes:
            add(causes, "normal_variance", "no rule-based defect found; loss consistent with strategy variance", 0.3)
    else:
        if tf.regime and (not suited or tf.regime in suited):
            add(strengths, "regime_aligned", f"{tf.regime} suits {codes or 'manual'}", 0.6)
        if tf.grade in ("A+", "A"):
            add(strengths, "high_confluence", f"grade {tf.grade}", 0.6)
        if tf.mae_r is not None and tf.mae_r < 0.3:
            add(strengths, "precise_entry", f"max adverse excursion only {tf.mae_r}R", 0.6)
        if tf.mfe_r and cp.r_multiple is not None and tf.mfe_r > 0:
            add(strengths, "exit_capture", f"captured {cp.r_multiple / tf.mfe_r:.0%} of max favourable move", 0.5)
    return {"causes": causes, "strengths": strengths}


def strategy_deterioration(db: Session, mode: str, codes: list[str], window: int = 20) -> dict:
    out = {}
    for code in codes:
        rows = db.execute(select(ClosedPosition.profit, ClosedPosition.commission, ClosedPosition.swap)
                          .where(ClosedPosition.mode == mode, ClosedPosition.strategy_codes.contains([code]), learnable())
                          .order_by(ClosedPosition.closed_at)).all()
        nets = [float(p) + float(c or 0) + float(s or 0) for p, c, s in rows]
        if len(nets) < 2 * window:
            continue
        pf = lambda x: (sum(v for v in x if v > 0) / -sum(v for v in x if v < 0)) if any(v < 0 for v in x) else None  # noqa: E731
        early, late = pf(nets[:-window]), pf(nets[-window:])
        if early and late is not None and early > 1.1 and late < 0.8:
            out[code] = {"deteriorating": True, "detail": f"profit factor {early:.2f} -> {late:.2f} over last {window} trades"}
    return out


def run(db: Session, mode: str, bridge=None, limit: int = 200) -> dict:
    done = {cid for (cid,) in db.execute(select(FailureReport.closed_position_id))}
    q = (select(ClosedPosition, TradeFeatures).join(TradeFeatures, TradeFeatures.closed_position_id == ClosedPosition.id)
         .where(TradeFeatures.mode == mode, learnable()).order_by(ClosedPosition.closed_at.desc()).limit(limit * 2))
    n = 0
    tally: dict[str, Counter] = defaultdict(Counter)
    for cp, tf in db.execute(q):
        if cp.id in done:
            continue
        post = None
        if bridge is not None and tf.outcome == "LOSS" and cp.close_reason == "STOP_LOSS":
            try:
                from app.scanner.service import bars_to_df
                bars = bars_to_df(bridge.rates(cp.symbol, "M15", 400))
                post = bars[(bars.index > pd.Timestamp(cp.closed_at)) & (bars.index <= pd.Timestamp(cp.closed_at) + pd.Timedelta(days=2))]
            except Exception:  # noqa: BLE001 - analysis degrades gracefully without post-trade bars
                post = None
        pred = None
        if tf.signal_id:
            mp = db.scalar(select(ModelPrediction).where(ModelPrediction.signal_id == tf.signal_id)
                           .order_by(ModelPrediction.id.desc()).limit(1))
            pred = mp.probability if mp else None
        res = analyse_trade(cp, tf, post, pred, strategy_deterioration(db, mode, cp.strategy_codes or []))
        db.add(FailureReport(closed_position_id=cp.id, mode=mode, outcome=tf.outcome, causes=res["causes"],
                             strengths=res["strengths"]))
        for c in res["causes"]:
            for code in (cp.strategy_codes or ["manual"]):
                tally[code][c["cause"]] += 1
        n += 1
        if n >= limit:
            break
    recs = 0
    for code, counts in tally.items():
        total = sum(counts.values())
        for cause, k in counts.most_common(3):
            remember(db, mode, "failure_pattern", f"{code}:{cause}", f"{cause} seen in {k} losing trades of {code}",
                     evidence_delta=k, payload={"cause": cause, "strategy": code})
            if k >= 5 and k / max(total, 1) >= 0.4 and cause != "normal_variance":
                db.add(AIRecommendation(source="failure", kind="investigate_failure_pattern", subject=code,
                                        recommendation=f"{cause} explains {k}/{total} loss causes for {code}. "
                                                       f"Run an isolated experiment (filter/parameter) — do not change "
                                                       f"production logic without validation.",
                                        evidence={"counts": dict(counts)}))
                recs += 1
    db.flush()
    return {"analysed": n, "recommendations": recs}


def missed_opportunities(db: Session, bridge, horizon_bars: int = 48, limit: int = 100) -> dict:
    done = {sid for (sid,) in db.execute(select(MissedOpportunity.signal_id))}
    executed = {sid for (sid,) in db.execute(select(TradeOrder.signal_id).where(TradeOrder.signal_id.isnot(None)))}
    sigs = db.scalars(select(TradingSignal).where(TradingSignal.decision.in_(["BUY", "SELL", "WAIT", "NO_TRADE"]),
                                                  TradingSignal.status.in_(["REJECTED", "EXPIRED"]),
                                                  TradingSignal.stop_loss.isnot(None), TradingSignal.tp2.isnot(None))
                      .order_by(TradingSignal.id.desc()).limit(limit * 2)).all()
    n = 0
    for s in sigs:
        if s.id in done or s.id in executed or not s.entry:
            continue
        sym = db.get(Symbol, s.symbol_id)
        outcome, r = "NO_DATA", None
        try:
            from app.scanner.service import bars_to_df
            bars = bars_to_df(bridge.rates(sym.broker_symbol, s.timeframe, horizon_bars + 600))
            after = bars[bars.index > pd.Timestamp(s.created_at)].iloc[:horizon_bars]
            if len(after) >= 3:
                d = 1 if float(s.tp2) > float(s.entry) else -1
                sl, tp = float(s.stop_loss), float(s.tp2)
                outcome = "NEITHER"
                for _, b in after.iterrows():
                    hit_sl = b.low <= sl if d > 0 else b.high >= sl
                    hit_tp = b.high >= tp if d > 0 else b.low <= tp
                    if hit_sl:
                        outcome, r = "SL_FIRST", -1.0
                        break
                    if hit_tp:
                        outcome, r = "TP_FIRST", s.risk_reward
                        break
        except Exception:  # noqa: BLE001
            outcome = "NO_DATA"
        reason = "; ".join((s.ai_review or {}).get("conflicts", [])[:2]) if s.ai_review else s.status
        db.add(MissedOpportunity(signal_id=s.id, rejection_reason=(reason or s.status)[:500],
                                 hypothetical_outcome=outcome, hypothetical_r=r))
        n += 1
        if n >= limit:
            break
    db.flush()
    return {"evaluated": n, "note": "hypothetical; excluded from performance statistics"}
