"""Performance analytics on *actual* net trading outcomes, with explicit statistical uncertainty.

Every metric block carries ``sample_size`` and a ``verdict``. With fewer than ``MIN_TRADES`` trades the
verdict is INSUFFICIENT_DATA regardless of how good the numbers look.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClosedPosition
from app.db.models_intel import PerformanceSnapshot, TradeFeatures


def learnable():
    """Positions whose MT5 deals reconcile. ``INCONSISTENT`` ones (see collector) never feed learning."""
    return ClosedPosition.sync_status.is_distinct_from("INCONSISTENT")


MIN_TRADES = 30
UTC = timezone.utc


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 4), round((c + m) / d, 4)


def bootstrap_ci(values: np.ndarray, fn, n_boot: int = 1000, seed: int = 7) -> tuple[float | None, float | None]:
    if len(values) < 5:
        return None, None
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        s = fn(rng.choice(values, size=len(values), replace=True))
        if s is not None and np.isfinite(s):
            stats.append(s)
    if len(stats) < 10:
        return None, None
    return round(float(np.percentile(stats, 2.5)), 4), round(float(np.percentile(stats, 97.5)), 4)


def _pf(x: np.ndarray) -> float | None:
    gl = -x[x < 0].sum()
    return float(x[x > 0].sum() / gl) if gl > 0 else None


def compute_metrics(trades: list[dict]) -> dict:
    """trades: dicts with net, r (optional), closed_at (datetime), mae_r, mfe_r (optional)."""
    n = len(trades)
    if n == 0:
        return {"sample_size": 0, "verdict": "NO_DATA"}
    trades = sorted(trades, key=lambda t: t["closed_at"])
    net = np.array([t["net"] for t in trades], float)
    rs = np.array([t["r"] for t in trades if t.get("r") is not None], float)
    wins, losses = net[net > 0], net[net < 0]
    eq = np.cumsum(net)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    max_dd = float((peak - eq).max()) if n else 0.0

    def streak(mask):
        best = cur = 0
        for v in mask:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    by_month: dict[str, float] = defaultdict(float)
    for t in trades:
        by_month[t["closed_at"].strftime("%Y-%m")] += t["net"]
    sharpe = sortino = None
    if len(rs) >= 5 and rs.std(ddof=1) > 0:
        sharpe = float(rs.mean() / rs.std(ddof=1))                    # per-trade, in R
        dn = rs[rs < 0]
        sortino = float(rs.mean() / dn.std(ddof=1)) if len(dn) >= 3 and dn.std(ddof=1) > 0 else None
    wr_lo, wr_hi = wilson(int((net > 0).sum()), n)
    exp_lo, exp_hi = bootstrap_ci(net, lambda x: float(x.mean()))
    pf_lo, pf_hi = bootstrap_ci(net, _pf)
    pf = _pf(net)
    maes = [t["mae_r"] for t in trades if t.get("mae_r") is not None]
    mfes = [t["mfe_r"] for t in trades if t.get("mfe_r") is not None]
    verdict = "INSUFFICIENT_DATA" if n < MIN_TRADES else (
        "POSITIVE_EDGE" if exp_lo is not None and exp_lo > 0 else ("NEGATIVE_EDGE" if exp_hi is not None and exp_hi < 0
                                                                     else "INCONCLUSIVE"))
    return {
        "sample_size": n, "verdict": verdict,
        "win_rate": round(float((net > 0).mean()), 4), "win_rate_ci95": [wr_lo, wr_hi],
        "loss_rate": round(float((net < 0).mean()), 4),
        "net_profit": round(float(net.sum()), 2), "gross_profit": round(float(wins.sum()), 2),
        "gross_loss": round(float(-losses.sum()), 2),
        "profit_factor": round(pf, 3) if pf is not None else None, "profit_factor_ci95": [pf_lo, pf_hi],
        "average_win": round(float(wins.mean()), 2) if len(wins) else None,
        "average_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "expectancy": round(float(net.mean()), 2), "expectancy_ci95": [exp_lo, exp_hi],
        "average_r": round(float(rs.mean()), 3) if len(rs) else None, "r_sample": int(len(rs)),
        "max_drawdown": round(max_dd, 2),
        "max_consecutive_wins": streak(net > 0), "max_consecutive_losses": streak(net < 0),
        "sharpe_per_trade": round(sharpe, 3) if sharpe is not None else None,
        "sortino_per_trade": round(sortino, 3) if sortino is not None else None,
        "recovery_factor": round(float(net.sum()) / max_dd, 3) if max_dd > 0 else None,
        "avg_mae_r": round(float(np.mean(maes)), 3) if maes else None,
        "avg_mfe_r": round(float(np.mean(mfes)), 3) if mfes else None,
        "monthly": {k: round(v, 2) for k, v in sorted(by_month.items())},
    }


def load_trades(db: Session, mode: str) -> list[dict]:
    q = select(ClosedPosition, TradeFeatures).outerjoin(TradeFeatures, TradeFeatures.closed_position_id == ClosedPosition.id) \
        .where((ClosedPosition.mode == mode) | ((ClosedPosition.mode.is_(None)) & (TradeFeatures.mode == mode))) \
        .where(learnable())
    out = []
    for cp, tf in db.execute(q):
        codes = sorted(cp.strategy_codes or [])
        vol_bucket = None
        if tf and tf.volatility_pct is not None:
            vol_bucket = "LOW" if tf.volatility_pct < 0.002 else ("HIGH" if tf.volatility_pct > 0.006 else "MEDIUM")
        out.append({
            "id": cp.id, "net": float(cp.profit) + float(cp.commission or 0) + float(cp.swap or 0),
            "r": cp.r_multiple, "closed_at": cp.closed_at, "symbol": cp.symbol, "strategies": codes,
            "combination": "+".join(codes) if len(codes) > 1 else None,
            "timeframe": tf.timeframe if tf else None, "session": tf.session if tf else None,
            "regime": (tf.regime if tf else None) or cp.regime, "volatility": vol_bucket,
            "entry_method": tf.entry_method if tf else None, "sl_method": tf.sl_method if tf else None,
            "tp_method": tf.tp_method if tf else None, "grade": tf.grade if tf else None,
            "model_version": ",".join(f"{k}:{v}" for k, v in sorted((tf.model_versions or {}).items())) if tf else None,
            "spread_bucket": (None if not tf or tf.spread_points is None else ("WIDE" if tf.spread_points > 20 else "NORMAL")),
            "mae_r": tf.mae_r if tf else None, "mfe_r": tf.mfe_r if tf else None,
        })
    return out


DIMENSIONS = ["strategy", "combination", "symbol", "timeframe", "session", "regime", "volatility", "entry_method",
              "sl_method", "tp_method", "model_version", "grade", "spread_bucket"]


def by_dimension(trades: list[dict]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for dim in DIMENSIONS:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            keys = t["strategies"] if dim == "strategy" else [t.get(dim)]
            for k in keys:
                if k:
                    groups[k].append(t)
        out[dim] = {k: compute_metrics(v) for k, v in groups.items()}
    return out


def strategy_correlation(trades: list[dict]) -> dict:
    daily: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        for s in t["strategies"]:
            daily[s][t["closed_at"].date()] += t["net"]
    names = sorted(s for s, d in daily.items() if len(d) >= 5)
    if len(names) < 2:
        return {"strategies": names, "matrix": [], "note": "need >= 2 strategies with >= 5 trading days"}
    days = sorted({d for s in names for d in daily[s]})
    m = np.array([[daily[s].get(d, 0.0) for d in days] for s in names])
    c = np.corrcoef(m)
    return {"strategies": names, "matrix": np.nan_to_num(c).round(3).tolist(), "days": len(days)}


def snapshot(db: Session, mode: str) -> dict:
    trades = load_trades(db, mode)
    now = datetime.now(UTC)
    rows = 0
    db.add(PerformanceSnapshot(mode=mode, dimension="overall", key="ALL", sample_size=len(trades),
                               metrics=compute_metrics(trades), computed_at=now))
    for dim, groups in by_dimension(trades).items():
        for key, m in groups.items():
            db.add(PerformanceSnapshot(mode=mode, dimension=dim, key=str(key)[:160], sample_size=m["sample_size"],
                                       metrics=m, computed_at=now))
            rows += 1
    db.flush()
    return {"mode": mode, "trades": len(trades), "snapshot_rows": rows + 1}


def latest(db: Session, mode: str, dimension: str, key: str | None = None) -> list[PerformanceSnapshot]:
    sub = select(PerformanceSnapshot.computed_at).where(PerformanceSnapshot.mode == mode).order_by(
        PerformanceSnapshot.computed_at.desc()).limit(1).scalar_subquery()
    q = select(PerformanceSnapshot).where(PerformanceSnapshot.mode == mode, PerformanceSnapshot.dimension == dimension,
                                          PerformanceSnapshot.computed_at == sub)
    if key is not None:
        q = q.where(PerformanceSnapshot.key == key)
    return list(db.scalars(q))
