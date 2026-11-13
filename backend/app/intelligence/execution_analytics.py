"""Execution quality analytics from the existing order/execution journal (no new source of truth).

An acknowledgement is not an execution: only rows in ``trade_executions`` (MT5-confirmed) count as fills;
rejections/unknowns come from ``trade_orders`` status.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import sessions as ss
from app.db.models import BrokerConnection, TradeExecution, TradeOrder, TradingAccount

REQUOTE, PARTIAL = 10004, 10010


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    fills = [r for r in rows if r["filled"]]
    slip = np.array([r["slippage"] for r in fills if r["slippage"] is not None], float)
    lat = np.array([r["latency"] for r in fills if r["latency"] is not None], float)
    pct = lambda a, q: round(float(np.percentile(a, q)), 2) if len(a) else None  # noqa: E731
    return {"orders": n, "fills": len(fills), "fill_rate": round(len(fills) / n, 4) if n else None,
            "rejections": sum(1 for r in rows if r["status"] in ("REJECTED", "FAILED")),
            "unknown_outcomes": sum(1 for r in rows if r["status"] == "UNKNOWN"),
            "requotes": sum(1 for r in rows if r["retcode"] == REQUOTE),
            "partial_fills": sum(1 for r in rows if r["retcode"] == PARTIAL),
            "slippage_points_mean": round(float(slip.mean()), 2) if len(slip) else None,
            "slippage_points_p95": pct(slip, 95), "latency_ms_p50": pct(lat, 50), "latency_ms_p95": pct(lat, 95)}


def analyse(db: Session, mode: str | None = None, limit: int = 5000) -> dict:
    q = (select(TradeOrder, TradeExecution, TradingAccount, BrokerConnection)
         .outerjoin(TradeExecution, TradeExecution.order_id == TradeOrder.id)
         .join(TradingAccount, TradingAccount.id == TradeOrder.account_id)
         .join(BrokerConnection, BrokerConnection.id == TradingAccount.broker_connection_id)
         .order_by(TradeOrder.id.desc()).limit(limit))
    if mode:
        q = q.where(TradingAccount.mode == mode)
    rows = []
    for o, e, a, b in db.execute(q):
        sess = ss.current_sessions(pd.Timestamp(o.created_at))
        rows.append({"filled": o.status == "CONFIRMED" and e is not None and e.retcode in (10008, 10009, 10010), "status": o.status,
                     "retcode": e.retcode if e else None, "slippage": e.slippage_points if e else None,
                     "latency": e.latency_ms if e else None, "broker": b.broker_name, "symbol": o.symbol,
                     "order_type": o.action, "strategy": ",".join(o.strategy_codes or []) or o.source,
                     "session": "+".join(s for s in sess if s in ("ASIA", "LONDON", "NEW_YORK")) or "OFF_SESSION",
                     "modification": o.action in ("MODIFY_SLTP", "MODIFY_PENDING")})
    out = {"overall": _stats(rows), "modification_failures": sum(1 for r in rows if r["modification"] and r["status"] in ("REJECTED", "FAILED"))}
    for dim in ("broker", "symbol", "session", "order_type", "strategy"):
        g = defaultdict(list)
        for r in rows:
            g[r[dim]].append(r)
        out[f"by_{dim}"] = {k: _stats(v) for k, v in g.items()}
    return out
