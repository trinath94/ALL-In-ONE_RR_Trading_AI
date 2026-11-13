"""Portfolio-level risk intelligence.

Supplies the existing Risk Engine with a *rolling, regime-dependent* correlation matrix (the engine
already accepts ``corr``) and produces exposure analytics + recommendations. It can recommend reducing
exposure or cause the Risk Engine's existing correlated-exposure check to reject a trade; it can never
raise a limit or approve a trade.
"""
from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AccountSnapshot, OpenPosition, Symbol, TradingAccount
from app.db.models_intel import PortfolioSnapshot
from app.services import trading as tr
from app.services.events import get_redis

CACHE_KEY = "rr:portfolio:corr"


def correlation_matrix(closes: dict[str, pd.Series], short: int = 100, long: int = 500) -> dict[tuple[str, str], float]:
    """Pairwise correlation of log returns; uses the larger |rho| of the short (current regime) and long
    windows so a relationship that tightened recently is not diluted by history."""
    rets = {k: np.log(v).diff().dropna() for k, v in closes.items() if len(v) > 30}
    out: dict[tuple[str, str], float] = {}
    for a, b in combinations(sorted(rets), 2):
        j = pd.concat([rets[a], rets[b]], axis=1, join="inner").dropna()
        if len(j) < 30:
            continue
        rs = j.iloc[-short:].corr().iloc[0, 1]
        rl = j.iloc[-long:].corr().iloc[0, 1]
        rho = rs if abs(rs) >= abs(rl) else rl
        if np.isfinite(rho):
            out[(a, b)] = round(float(rho), 4)
    return out


def cache_corr(corr: dict[tuple[str, str], float], ttl: int = 3600) -> None:
    get_redis().set(CACHE_KEY, json.dumps({f"{a}|{b}": v for (a, b), v in corr.items()}), ex=ttl)


def cached_corr() -> dict[tuple[str, str], float]:
    try:
        raw = get_redis().get(CACHE_KEY)
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    return {tuple(k.split("|")): v for k, v in json.loads(raw).items()}


def refresh_corr(db: Session, bridge, extra_symbols: list[str] | None = None, timeframe: str = "H1") -> dict:
    from app.scanner.service import bars_to_df
    acct = tr.active_account(db)
    syms = set(extra_symbols or [])
    if acct:
        syms |= {p.symbol for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == acct.id))}
    syms |= {s.broker_symbol for s in db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True)).limit(40))}
    closes = {}
    for s in syms:
        try:
            closes[s] = bars_to_df(bridge.rates(s, timeframe, 600))["close"]
        except Exception:  # noqa: BLE001
            continue
    corr = correlation_matrix(closes)
    cache_corr(corr)
    return {"symbols": len(closes), "pairs": len(corr)}


def assess(db: Session, account: TradingAccount, corr: dict[tuple[str, str], float] | None = None) -> dict:
    corr = corr if corr is not None else cached_corr()
    snap = db.scalar(select(AccountSnapshot).where(AccountSnapshot.account_id == account.id).order_by(AccountSnapshot.id.desc()).limit(1))
    eq = float(snap.equity) if snap else 0.0
    positions = db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id)).all()
    by_symbol, by_strategy, by_ccy, by_dir = defaultdict(float), defaultdict(float), defaultdict(float), defaultdict(float)
    risks, names = [], []
    for p in positions:
        r = tr.position_risk(db, p)
        sgn = 1 if p.direction == "BUY" else -1
        by_symbol[p.symbol] += r.risk_amount
        by_dir[p.direction] += r.risk_amount
        for s in (p.strategy_codes or ["manual/external"]):
            by_strategy[s] += r.risk_amount / max(len(p.strategy_codes or [1]), 1)
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbol))
        if sym and sym.base_currency:
            by_ccy[sym.base_currency] += sgn * r.risk_amount
        if sym and sym.profit_currency:
            by_ccy[sym.profit_currency] -= sgn * r.risk_amount
        risks.append(sgn * r.risk_amount)
        names.append(p.symbol)
    total = sum(abs(x) for x in risks)
    eff_bets = None
    clusters = []
    if len(risks) >= 2:
        C = np.eye(len(risks))
        for i, j in combinations(range(len(risks)), 2):
            rho = corr.get((names[i], names[j]), corr.get((names[j], names[i]), 0.0 if names[i] != names[j] else 1.0))
            C[i, j] = C[j, i] = rho
        w = np.abs(np.array(risks)) / total if total else np.zeros(len(risks))
        s = np.sign(risks)
        signed = np.outer(s, s) * C
        denom = float(w @ signed @ w)
        eff_bets = round(1 / denom, 2) if denom > 0 else None
        for i, j in combinations(range(len(risks)), 2):
            if signed[i, j] > 0.7:
                clusters.append([names[i], names[j], round(float(C[i, j]), 3)])
    recs = []
    if total and max(by_symbol.values(), default=0) / total > 0.5 and len(by_symbol) > 1:
        recs.append("Over 50% of open risk sits in one symbol — consider reducing concentration.")
    if eff_bets is not None and len(risks) > 2 and eff_bets < 1.5:
        recs.append(f"Open positions behave like {eff_bets} independent bets — they represent largely the same underlying risk.")
    if clusters:
        recs.append(f"Highly correlated same-direction exposure: {clusters}")
    dir_total = by_dir.get("BUY", 0) + by_dir.get("SELL", 0)
    if dir_total and max(by_dir.values()) / dir_total > 0.85 and len(positions) >= 3:
        recs.append("Directional concentration above 85% of open risk.")
    margin_pct = float(snap.margin) / eq * 100 if snap and eq else None
    metrics = {"equity": eq, "open_positions": len(positions), "total_open_risk": round(total, 2),
               "total_open_risk_pct": round(total / eq * 100, 3) if eq else None,
               "risk_by_symbol": {k: round(v, 2) for k, v in by_symbol.items()},
               "risk_by_strategy": {k: round(v, 2) for k, v in by_strategy.items()},
               "net_risk_by_currency": {k: round(v, 2) for k, v in by_ccy.items()},
               "risk_by_direction": {k: round(v, 2) for k, v in by_dir.items()},
               "effective_independent_bets": eff_bets, "correlated_clusters": clusters,
               "margin_utilisation_pct": round(margin_pct, 2) if margin_pct is not None else None}
    return {"metrics": metrics, "recommendations": recs,
            "correlation": {f"{a}|{b}": v for (a, b), v in corr.items() if a in names or b in names}}


def snapshot(db: Session, account: TradingAccount) -> PortfolioSnapshot:
    a = assess(db, account)
    s = PortfolioSnapshot(account_id=account.id, metrics=a["metrics"], correlation=a["correlation"],
                          recommendations=a["recommendations"])
    db.add(s)
    db.flush()
    return s
