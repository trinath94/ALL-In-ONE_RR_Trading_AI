"""Enrich closed positions with decision context and trade-quality measurements (``trade_features``)."""
from __future__ import annotations

import logging
from datetime import timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import sessions as ss
from app.db.models import (AgentDecision, AnalysisResult, ClosedPosition, Symbol, TradeExecution, TradeOrder,
                           TradingAccount, TradingSignal)
from app.db.models_intel import TradeFeatures
from app.execution.bridge import BridgeClient, BridgeError
from app.learning_engine.performance import learnable

log = logging.getLogger("learning.features")
UTC = timezone.utc
TF_MIN = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def excursions(bars: pd.DataFrame, direction: str, entry: float, stop: float | None) -> tuple[float | None, float | None]:
    """MAE/MFE in R from price bars spanning the trade (bid OHLC). Requires a stop to define R."""
    if bars is None or bars.empty or not stop:
        return None, None
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None
    if direction == "BUY":
        mae = (entry - bars["low"].min()) / risk
        mfe = (bars["high"].max() - entry) / risk
    else:
        mae = (bars["high"].max() - entry) / risk
        mfe = (entry - bars["low"].min()) / risk
    return round(max(float(mae), 0.0), 4), round(max(float(mfe), 0.0), 4)


def classify_outcome(net: float, r: float | None) -> str:
    if r is not None and abs(r) < 0.1:
        return "BE"
    return "WIN" if net > 0 else ("LOSS" if net < 0 else "BE")


def enrich(db: Session, cp: ClosedPosition, bridge: BridgeClient | None = None) -> TradeFeatures:
    with db.no_autoflush:
        return _enrich(db, cp, bridge)


def _enrich(db: Session, cp: ClosedPosition, bridge: BridgeClient | None = None) -> TradeFeatures:
    tf = db.scalar(select(TradeFeatures).where(TradeFeatures.closed_position_id == cp.id))
    is_new = tf is None
    if is_new:
        tf = TradeFeatures(closed_position_id=cp.id)   # added to the session only once fully populated
    acct = db.get(TradingAccount, cp.account_id)
    quality: dict = {}
    order = db.scalar(select(TradeOrder).where(TradeOrder.execution_id == cp.execution_id)) if cp.execution_id else None
    sig = db.get(TradingSignal, order.signal_id) if order and order.signal_id else None
    ana = db.get(AnalysisResult, sig.analysis_id) if sig and sig.analysis_id else None
    exe = db.scalar(select(TradeExecution).where(TradeExecution.order_id == order.id)) if order else None
    dec = db.scalar(select(AgentDecision).where(AgentDecision.signal_id == sig.id)) if sig else None
    sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == cp.symbol))
    net = float(cp.profit) + float(cp.commission or 0) + float(cp.swap or 0)

    tf.mode = cp.mode or (acct.mode if acct else "DEMO")
    tf.symbol = cp.symbol
    tf.timeframe = sig.timeframe if sig else None
    sess = ss.current_sessions(pd.Timestamp(cp.opened_at))
    tf.session = "+".join(s for s in sess if s in ("ASIA", "LONDON", "NEW_YORK", "OVERLAP")) or "OFF_SESSION"
    tf.regime = (ana.regime if ana else None) or cp.regime
    struct = (ana.structure or {}) if ana else {}
    tf.news_status = struct.get("news")
    tf.signal_id = sig.id if sig else None
    tf.analysis_id = ana.id if ana else None
    tf.agent_conversation_id = dec.conversation_id if dec else None
    tf.confluence_score = sig.score if sig else None
    tf.grade = sig.grade if sig else None
    tf.model_versions = (sig.ai_review or {}).get("model_versions", {}) if sig and sig.ai_review else {}
    tf.gross_profit = float(cp.profit)
    tf.net_profit = round(net, 2)
    tf.partial_exits = cp.partial_exits or 1
    tf.holding_seconds = int((cp.closed_at - cp.opened_at).total_seconds())
    tf.r_multiple = cp.r_multiple
    tf.slippage_points = exe.slippage_points if exe else None
    tf.entry_method = (sig.strategies[0].get("strategy") if sig and sig.strategies else ("MANUAL" if order else "EXTERNAL"))
    tf.sl_method = "STRUCTURAL" if sig else ("MANUAL" if order else "UNKNOWN")
    tf.tp_method = "R_MULTIPLE" if sig else ("MANUAL" if order else "UNKNOWN")
    if sym and cp.stop_loss and float(sym.tick_size) > 0:
        per_unit = float(sym.tick_value) / float(sym.tick_size)
        tf.initial_risk = round(abs(float(cp.open_price) - float(cp.stop_loss)) * per_unit * float(cp.volume), 2)
    else:
        quality["initial_risk"] = "unavailable (no stop or symbol spec)"
    tf.spread_points = struct.get("spread_points")
    if struct.get("atr") and struct.get("price"):
        tf.volatility_pct = round(float(struct["atr"]) / float(struct["price"]), 6)
    elif ana:
        quality["volatility"] = "analysis predates ATR capture"
    tf.outcome = classify_outcome(net, cp.r_multiple)
    # MAE / MFE from broker bars covering the trade
    if bridge is not None and cp.stop_loss:
        try:
            dur_min = max(tf.holding_seconds / 60, 1)
            tfname = "M1" if dur_min <= 600 else ("M5" if dur_min <= 3000 else "H1")
            count = int(min(5000, dur_min / TF_MIN[tfname] + 400))
            from app.scanner.service import bars_to_df
            bars = bars_to_df(bridge.rates(cp.symbol, tfname, count))
            win = bars[(bars.index >= pd.Timestamp(cp.opened_at).floor(f"{TF_MIN[tfname]}min")) & (bars.index <= pd.Timestamp(cp.closed_at))]
            if len(win):
                tf.mae_r, tf.mfe_r = excursions(win, cp.direction, float(cp.open_price), float(cp.stop_loss))
            else:
                quality["mae_mfe"] = f"no {tfname} bars covering trade (history depth)"
        except BridgeError as exc:
            quality["mae_mfe"] = f"bridge unavailable: {exc}"[:200]
    elif not cp.stop_loss:
        quality["mae_mfe"] = "unavailable (no initial stop)"
    else:
        quality["mae_mfe"] = "pending (no bridge)"
    tf.data_quality = quality
    if is_new:
        db.add(tf)
    db.flush()
    return tf


def enrich_pending(db: Session, bridge: BridgeClient | None, limit: int = 200) -> int:
    done = {cid for (cid,) in db.execute(select(TradeFeatures.closed_position_id))}
    rows = db.scalars(select(ClosedPosition).where(learnable()).order_by(ClosedPosition.closed_at.desc()).limit(limit * 3)).all()
    n = 0
    for cp in rows:
        if cp.id in done:
            continue
        enrich(db, cp, bridge)
        n += 1
        if n >= limit:
            break
    return n
