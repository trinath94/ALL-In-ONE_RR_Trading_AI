"""Market data, scanner, strategies, backtesting, risk config, portfolio, news, notifications."""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, require
from app.api.routes.trading import bridge_dep, f
from app.backtesting import engine as bt
from app.db.models import (AnalysisResult, BacktestJob, BacktestResult, ClosedPosition, EconomicEvent, Notification,
                           NotificationSetting, OpenPosition, OptimisationResult, RiskConfig, RiskEvaluation, Strategy,
                           StrategyConfiguration, StrategyExperiment, StrategyPerformance, StrategyVersion, Symbol, User)
from app.db.session import get_db, session_scope
from app.execution.bridge import BridgeClient, BridgeError
from app.news import service as news
from app.notifications.service import KINDS
from app.risk.engine import RiskParams
from app.scanner.service import bars_to_df
from app.services import trading as tr
from app.services.events import audit
from app.strategies import registry

router = APIRouter(prefix="/api", tags=["intelligence"])
UTC = timezone.utc
TFS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"]


# ---- symbols & market ------------------------------------------------------------------------
@router.get("/symbols")
def symbols(_: User = Depends(current_user), db: Session = Depends(get_db), category: str | None = None):
    q = select(Symbol).order_by(Symbol.priority, Symbol.canonical)
    if category:
        q = q.where(Symbol.category == category)
    return [{"id": s.id, "symbol": s.broker_symbol, "canonical": s.canonical, "category": s.category,
             "description": s.description, "digits": s.digits, "tradable": s.tradable, "scan_enabled": s.scan_enabled,
             "priority": s.priority, "volume_min": f(s.volume_min), "volume_step": f(s.volume_step),
             "contract_size": f(s.contract_size)} for s in db.scalars(q)]


@router.post("/symbols/discover")
def discover(user: User = Depends(require("system.manage")), db: Session = Depends(get_db),
             bridge: BridgeClient = Depends(bridge_dep)):
    acct = tr.active_account(db)
    if not acct:
        raise HTTPException(409, "Configure and activate an account first")
    res = tr.discover_symbols(db, bridge, acct.broker_connection_id)
    audit(db, user.id, "SYMBOL_DISCOVERY", "", res)
    return res


class SymbolPatch(BaseModel):
    scan_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


@router.patch("/symbols/{symbol_id}")
def patch_symbol(symbol_id: int, body: SymbolPatch, user: User = Depends(require("strategy.manage")),
                 db: Session = Depends(get_db)):
    s = db.get(Symbol, symbol_id)
    if not s:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    audit(db, user.id, "SYMBOL_UPDATE", s.broker_symbol, body.model_dump(exclude_none=True))
    return {"ok": True}


@router.get("/market/{symbol}/rates")
def rates(symbol: str, timeframe: str = "H1", count: int = 500, _: User = Depends(current_user),
          bridge: BridgeClient = Depends(bridge_dep)):
    if timeframe not in TFS:
        raise HTTPException(400, "bad timeframe")
    try:
        rows = bridge.rates(symbol, timeframe, min(max(count, 10), 5000))
    except BridgeError as exc:
        raise HTTPException(502, str(exc)) from None
    return [{"time": int(r["time"]), "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r.get("real_volume") or r.get("tick_volume", 0), "volume_type": "real" if r.get("real_volume") else "tick"}
            for r in rows]


@router.get("/market/{symbol}/tick")
def tick(symbol: str, _: User = Depends(current_user), bridge: BridgeClient = Depends(bridge_dep)):
    return bridge.tick(symbol)


@router.get("/market/{symbol}/overlays")
def overlays(symbol: str, timeframe: str = "H1", _: User = Depends(current_user),
             bridge: BridgeClient = Depends(bridge_dep)):
    """Structure overlays for the chart: swings, BOS/CHOCH, FVGs, order blocks, liquidity, fib levels."""
    from app.analysis import indicators as ind
    from app.analysis import structure as st
    df = bars_to_df(bridge.rates(symbol, timeframe, 500))
    if len(df) < 60:
        return {}
    t = lambda i: int(df.index[i].timestamp())  # noqa: E731
    ev, trend = st.structure_events(df)
    fvgs = [g for g in st.fair_value_gaps(df, 0.2) if g.filled_idx is None][-15:]
    obs = [z for z in st.order_blocks(df) if z.broken_idx is None][-10:]
    sw = st.swings(df, 3)
    tol = 0.1 * float(ind.atr(df).iloc[-1])
    eqh = st.equal_levels(sw, "H", tol)[-5:]
    eql = st.equal_levels(sw, "L", tol)[-5:]
    hs = [s for s in sw if s.kind == "H"]
    ls = [s for s in sw if s.kind == "L"]
    fib = None
    if hs and ls:
        hi, lo = hs[-1], ls[-1]
        rng = hi.price - lo.price
        up = lo.idx < hi.idx
        fib = {str(r): (hi.price - r * rng) if up else (lo.price + r * rng) for r in (0, 0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1)}
    c = df.close
    return {
        "trend": trend,
        "events": [{"time": t(e.idx), "kind": e.kind, "direction": e.direction, "level": e.level} for e in ev[-20:]],
        "fvgs": [{"time": t(g.idx), "direction": g.direction, "top": g.top, "bottom": g.bottom} for g in fvgs],
        "order_blocks": [{"time": t(z.idx), "direction": z.direction, "top": z.top, "bottom": z.bottom} for z in obs],
        "liquidity": [{"kind": "EQH", "price": max(a.price, b.price), "time": t(b.idx)} for a, b in eqh] +
                     [{"kind": "EQL", "price": min(a.price, b.price), "time": t(b.idx)} for a, b in eql],
        "fibonacci": fib,
        "indicators": {k: [{"time": t(i), "value": float(v)} for i, v in enumerate(s) if not np.isnan(v)][-300:] for k, s in {
            "ema20": ind.ema(c, 20), "ema50": ind.ema(c, 50), "ema200": ind.ema(c, 200), "sma50": ind.sma(c, 50),
            "rsi": ind.rsi(c), "atr": ind.atr(df), "vwap": ind.vwap_session(df), "macd": ind.macd(c)["macd"],
            "macd_signal": ind.macd(c)["signal"], "macd_hist": ind.macd(c)["hist"]}.items()},
    }


@router.get("/scanner")
def scanner(_: User = Depends(current_user), db: Session = Depends(get_db), timeframe: str | None = None):
    """Latest analysis per symbol/timeframe (real scan output)."""
    sub = select(AnalysisResult.symbol_id, AnalysisResult.timeframe, func.max(AnalysisResult.id).label("mid")) \
        .group_by(AnalysisResult.symbol_id, AnalysisResult.timeframe).subquery()
    q = select(AnalysisResult, Symbol).join(sub, AnalysisResult.id == sub.c.mid).join(Symbol, Symbol.id == AnalysisResult.symbol_id)
    if timeframe:
        q = q.where(AnalysisResult.timeframe == timeframe)
    out = []
    for a, s in db.execute(q):
        sc = a.score or {}
        out.append({"symbol": s.broker_symbol, "canonical": s.canonical, "category": s.category, "timeframe": a.timeframe,
                    "bar_time": a.bar_time, "regime": a.regime, "decision": sc.get("decision"), "grade": sc.get("grade"),
                    "score": sc.get("score"), "direction": sc.get("direction"), "entry": sc.get("entry"),
                    "stop_loss": sc.get("stop_loss"), "take_profit": sc.get("tp2"), "risk_reward": sc.get("risk_reward"),
                    "strategies": sorted({x["strategy"] for x in sc.get("strategies", [])}),
                    "htf_bias": (a.structure or {}).get("htf_bias"), "news": (a.structure or {}).get("news"),
                    "analysed_at": a.created_at})
    return sorted(out, key=lambda r: -(r["score"] or 0))


# ---- strategies -----------------------------------------------------------------------------------
@router.get("/strategies")
def strategies(_: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = {s.code: s for s in db.scalars(select(Strategy))}
    perf: dict[int, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "gp": 0.0, "gl": 0.0, "avg_r": [], "dd": 0.0})
    for p in db.scalars(select(StrategyPerformance)):
        d = perf[p.strategy_id]
        d["trades"] += p.trades
        d["wins"] += p.wins
        d["gp"] += p.gross_profit
        d["gl"] += p.gross_loss
        d["avg_r"].append((p.avg_r, p.trades))
        d["dd"] = max(d["dd"], p.max_drawdown)
    live = defaultdict(int)
    for codes, in db.execute(select(OpenPosition.strategy_codes)):
        for c in codes or []:
            live[c] += 1
    out = []
    for m in registry.metas():
        s = rows.get(m.code)
        d = perf.get(s.id) if s else None
        tw = sum(t for _, t in d["avg_r"]) if d else 0
        out.append({**m.__dict__, "enabled": bool(s and s.enabled), "stage": s.stage if s else "UNREGISTERED",
                    "availability": s.availability if s else "unknown", "active_positions": live.get(m.code, 0),
                    "performance": None if not d or not d["trades"] else {
                        "trades": d["trades"], "win_rate": round(d["wins"] / d["trades"], 4),
                        "profit_factor": round(d["gp"] / d["gl"], 3) if d["gl"] else None,
                        "avg_r": round(sum(r * t for r, t in d["avg_r"]) / tw, 3) if tw else None,
                        "max_drawdown": d["dd"]}})
    return out


class StrategyPatch(BaseModel):
    enabled: bool | None = None
    params: dict | None = None
    mode: str = Field(default="DEMO", pattern="^(DEMO|LIVE)$")


@router.patch("/strategies/{code}")
def patch_strategy(code: str, body: StrategyPatch, user: User = Depends(require("strategy.manage")),
                   db: Session = Depends(get_db)):
    s = db.scalar(select(Strategy).where(Strategy.code == code))
    if not s:
        raise HTTPException(404)
    cls = registry.get(code)
    if body.enabled and cls.requires and s.availability != "available":
        raise HTTPException(409, f"Strategy unavailable: requires {cls.requires}")
    if body.params is not None:
        unknown = set(body.params) - set(cls.params)
        if unknown:
            raise HTTPException(400, f"Unknown parameters {sorted(unknown)}")
    cfg = db.scalar(select(StrategyConfiguration).where(StrategyConfiguration.strategy_id == s.id,
                                                        StrategyConfiguration.account_mode == body.mode,
                                                        StrategyConfiguration.symbol_id.is_(None)))
    if cfg is None:
        cfg = StrategyConfiguration(strategy_id=s.id, account_mode=body.mode, enabled=True, params={}, timeframes=[])
        db.add(cfg)
    if body.enabled is not None:
        if body.mode == "LIVE" and body.enabled and cls.risky:
            raise HTTPException(409, "Risky strategies (grid/martingale/DCA) cannot be enabled for LIVE from the UI")
        cfg.enabled = body.enabled
        if body.mode == "DEMO":
            s.enabled = body.enabled or s.enabled
    if body.params is not None:
        cfg.params = body.params
    audit(db, user.id, "STRATEGY_UPDATE", code, body.model_dump(exclude_none=True))
    return {"ok": True}


@router.get("/strategies/{code}/versions")
def versions(code: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.scalar(select(Strategy).where(Strategy.code == code))
    if not s:
        raise HTTPException(404)
    return [{"version": v.version, "code_hash": v.code_hash, "notes": v.notes, "by": v.created_by, "at": v.created_at}
            for v in db.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == s.id).order_by(StrategyVersion.id))]


@router.get("/strategies-analytics/correlation")
def correlation(_: User = Depends(current_user), db: Session = Depends(get_db), days: int = 90):
    """Correlation of daily realised P&L between strategies (closed trades)."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(ClosedPosition.closed_at, ClosedPosition.profit, ClosedPosition.strategy_codes)
                      .where(ClosedPosition.closed_at >= since)).all()
    recs = [{"day": c.date(), "strategy": s, "pnl": p} for c, p, codes in rows for s in (codes or [])]
    if not recs:
        return {"strategies": [], "matrix": [], "note": "no closed trades in range"}
    pv = pd.DataFrame(recs).pivot_table(index="day", columns="strategy", values="pnl", aggfunc="sum").fillna(0)
    cm = pv.corr().round(3).fillna(0)
    return {"strategies": list(cm.columns), "matrix": cm.values.tolist(), "days": len(pv)}


@router.get("/strategies-analytics/monthly")
def monthly(_: User = Depends(current_user), db: Session = Depends(get_db)):
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c, p, codes in db.execute(select(ClosedPosition.closed_at, ClosedPosition.profit, ClosedPosition.strategy_codes)):
        for s in codes or []:
            out[s][c.strftime("%Y-%m")] += p
    return {k: dict(v) for k, v in out.items()}


@router.get("/experiments")
def experiments(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": e.id, "title": e.title, "hypothesis": e.hypothesis, "proposal": e.proposal, "stage": e.stage,
             "evidence": e.evidence, "created_at": e.created_at}
            for e in db.scalars(select(StrategyExperiment).order_by(StrategyExperiment.id.desc()))]


class StageIn(BaseModel):
    stage: str
    evidence: dict = Field(default_factory=dict)


@router.post("/experiments/{exp_id}/stage")
def experiment_stage(exp_id: int, body: StageIn, user: User = Depends(require("strategy.manage")),
                     db: Session = Depends(get_db)):
    """Backward compatible: legacy stage names (RESEARCH, IMPLEMENTATION, ...) are mapped onto the Part 25
    lifecycle. Evidence gates are enforced by app.strategies.experimental.lifecycle."""
    from app.strategies.experimental import lifecycle
    e = db.get(StrategyExperiment, exp_id)
    if not e:
        raise HTTPException(404, "experiment not found")
    try:
        lifecycle.transition(db, e, body.stage, user.id, json_notes(body.evidence))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(409, str(exc)) from None
    e.evidence = {**(e.evidence or {}), e.stage: {**body.evidence, "by": user.username, "at": datetime.now(UTC).isoformat()}}
    if e.stage == "APPROVED":
        e.approved_by = user.id
    audit(db, user.id, "EXPERIMENT_STAGE", e.title, {"stage": e.stage})
    return {"ok": True, "stage": e.stage}


def json_notes(d: dict) -> str:
    import json
    return json.dumps(d, default=str)[:2000] if d else ""


# ---- backtesting ------------------------------------------------------------------------------------
class BacktestIn(BaseModel):
    kind: str = Field(default="BACKTEST", pattern="^(BACKTEST|OOS|WALKFORWARD|MONTECARLO|OPTIMISE|SPREAD)$")
    symbol: str
    timeframe: str = "H1"
    strategies: list[str] = Field(min_length=1, max_length=50)
    start: datetime | None = None
    end: datetime | None = None
    bars: int = Field(default=5000, ge=500, le=50000)
    initial_balance: float = Field(default=10000, gt=0)
    risk_pct: float = Field(default=1.0, gt=0, le=5)
    sizing: str = Field(default="risk", pattern="^(risk|fixed)$")
    fixed_lots: float = 0.1
    spread_points: float | None = None
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0
    use_signal_engine: bool = False
    htf: str | None = None
    grid: dict[str, list] | None = None
    params: dict[str, dict] = Field(default_factory=dict)


def _run_job(job_id: int) -> None:
    with session_scope() as db:
        job = db.get(BacktestJob, job_id)
        job.status = "RUNNING"
        p = BacktestIn(**job.params)
    try:
        bridge = BridgeClient()
        with session_scope() as db:
            sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbol))
        df = bars_to_df(bridge.rates(p.symbol, p.timeframe, p.bars))
        if p.start:
            df = df[df.index >= pd.Timestamp(p.start)]
        if p.end:
            df = df[df.index <= pd.Timestamp(p.end)]
        if len(df) < 400:
            raise ValueError(f"only {len(df)} bars of broker history available")
        spec = bt.InstrumentSpec(p.symbol, float(sym.point), float(sym.tick_size), float(sym.tick_value),
                                 float(sym.volume_min), float(sym.volume_step), float(sym.volume_max)) if sym else bt.InstrumentSpec(p.symbol)
        cfg = bt.BacktestConfig(p.strategies, p.timeframe, p.initial_balance, p.risk_pct, p.sizing, p.fixed_lots,
                                use_signal_engine=p.use_signal_engine, htf=p.htf, params=p.params,
                                costs=bt.CostModel(p.spread_points, p.commission_per_lot, p.slippage_points), spec=spec)
        results: list[tuple[str, dict]] = []
        opt_rows = []
        if p.kind == "BACKTEST":
            results.append(("FULL", bt.run_backtest(df, cfg)))
        elif p.kind == "OOS":
            r = bt.split_oos(df, cfg)
            results += [("IN_SAMPLE", r["in_sample"]), ("OOS", r["out_of_sample"])]
        elif p.kind == "MONTECARLO":
            r = bt.run_backtest(df, cfg)
            r["monte_carlo"] = bt.monte_carlo(r["trades"], p.initial_balance)
            results.append(("FULL", r))
        elif p.kind == "SPREAD":
            base = p.spread_points if p.spread_points is not None else float(df["spread"].median()) if "spread" in df else 10
            results.append(("SPREAD", {"metrics": {"sensitivity": bt.spread_sensitivity(df, cfg, base)},
                                       "trades": [], "equity_curve": []}))
        elif p.kind in ("OPTIMISE", "WALKFORWARD"):
            if len(p.strategies) != 1 or not p.grid:
                raise ValueError("optimisation needs exactly one strategy and a parameter grid")
            if p.kind == "OPTIMISE":
                opt_rows = bt.optimise(df, cfg, p.strategies[0], p.grid)
            else:
                wf = bt.walk_forward(df, cfg, p.strategies[0], p.grid)
                results.append(("WALKFORWARD", {"metrics": wf["combined_oos"], "trades": [], "equity_curve": [],
                                                "windows": wf["windows"]}))
        with session_scope() as db:
            for seg, r in results:
                metrics = dict(r["metrics"])
                if "monte_carlo" in r:
                    metrics["monte_carlo"] = r["monte_carlo"]
                if "windows" in r:
                    metrics["windows"] = r["windows"]
                db.add(BacktestResult(job_id=job_id, segment=seg, metrics=_json_safe(metrics),
                                      equity_curve=r["equity_curve"][-5000:], trades=r["trades"][-5000:]))
                if seg in ("OOS", "WALKFORWARD") and len(p.strategies) == 1:
                    _record_perf(db, p.strategies[0], p.symbol, p.timeframe, seg, r)
            for row in opt_rows:
                db.add(OptimisationResult(job_id=job_id, params=row["params"], in_sample=_json_safe(row["in_sample"]),
                                          out_of_sample=_json_safe(row.get("out_of_sample") or {}), rank=row["rank"]))
            job = db.get(BacktestJob, job_id)
            job.status, job.progress = "DONE", 1.0
        bridge.close()
    except Exception as exc:  # noqa: BLE001
        with session_scope() as db:
            job = db.get(BacktestJob, job_id)
            job.status, job.error = "FAILED", str(exc)[:2000]


def _json_safe(d):
    import json
    return json.loads(json.dumps(d, default=str).replace("Infinity", "null"))


def _record_perf(db: Session, code: str, symbol: str, tf: str, seg: str, r: dict) -> None:
    s = db.scalar(select(Strategy).where(Strategy.code == code))
    if not s:
        return
    m = r["metrics"]
    tr_ = r.get("trades") or []
    db.add(StrategyPerformance(strategy_id=s.id, symbol=symbol, timeframe=tf, source="OOS" if seg == "OOS" else "WALKFORWARD",
                               trades=m["total_trades"], wins=m["winning_trades"], gross_profit=m["gross_profit"],
                               gross_loss=m["gross_loss"], avg_r=m["avg_r"] or 0, max_drawdown=m["max_drawdown_pct"],
                               period_start=pd.Timestamp(tr_[0]["entry_time"]).to_pydatetime() if tr_ else None,
                               period_end=pd.Timestamp(tr_[-1]["exit_time"]).to_pydatetime() if tr_ else None))


@router.post("/backtests")
def create_backtest(body: BacktestIn, user: User = Depends(require("backtest.run")), db: Session = Depends(get_db)):
    unknown = [c for c in body.strategies if c not in registry.all_strategies()]
    if unknown:
        raise HTTPException(400, f"unknown strategies {unknown}")
    running = db.scalar(select(func.count(BacktestJob.id)).where(BacktestJob.status.in_(["QUEUED", "RUNNING"])))
    if running >= 3:
        raise HTTPException(429, "Too many backtests running")
    job = BacktestJob(kind=body.kind, params=body.model_dump(mode="json"), requested_by=user.id)
    db.add(job)
    db.commit()
    threading.Thread(target=_run_job, args=(job.id,), daemon=True).start()
    return {"id": job.id, "status": job.status}


@router.get("/backtests")
def list_backtests(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": j.id, "kind": j.kind, "status": j.status, "params": j.params, "error": j.error, "created_at": j.created_at}
            for j in db.scalars(select(BacktestJob).order_by(BacktestJob.id.desc()).limit(100))]


@router.get("/backtests/{job_id}")
def get_backtest(job_id: int, _: User = Depends(current_user), db: Session = Depends(get_db)):
    j = db.get(BacktestJob, job_id)
    if not j:
        raise HTTPException(404)
    return {"id": j.id, "kind": j.kind, "status": j.status, "params": j.params, "error": j.error,
            "results": [{"segment": r.segment, "metrics": r.metrics, "equity_curve": r.equity_curve, "trades": r.trades}
                        for r in db.scalars(select(BacktestResult).where(BacktestResult.job_id == j.id))],
            "optimisation": [{"rank": o.rank, "params": o.params, "in_sample": o.in_sample, "out_of_sample": o.out_of_sample}
                             for o in db.scalars(select(OptimisationResult).where(OptimisationResult.job_id == j.id)
                                                 .order_by(OptimisationResult.rank))]}


# ---- risk --------------------------------------------------------------------------------------
@router.get("/risk/config/{mode}")
def get_risk(mode: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    if mode not in ("DEMO", "LIVE"):
        raise HTTPException(400)
    return tr.risk_params(db, mode).__dict__


@router.put("/risk/config/{mode}")
def put_risk(mode: str, body: dict, user: User = Depends(require("risk.manage")), db: Session = Depends(get_db)):
    if mode not in ("DEMO", "LIVE"):
        raise HTTPException(400)
    unknown = set(body) - set(RiskParams().__dict__)
    if unknown:
        raise HTTPException(400, f"unknown parameters {sorted(unknown)}")
    params = RiskParams.from_dict({**tr.risk_params(db, mode).__dict__, **body})
    errs = params.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == mode))
    if rc is None:
        rc = RiskConfig(account_mode=mode, params={})
        db.add(rc)
    old = dict(rc.params or {})
    rc.params, rc.updated_by = params.__dict__, user.id
    audit(db, user.id, "RISK_CONFIG_UPDATE", mode, {"old": old, "new": params.__dict__})
    return params.__dict__


@router.get("/risk/evaluations")
def evaluations(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 100):
    return [{"id": e.id, "signal_id": e.signal_id, "approved": e.approved, "volume": f(e.volume),
             "risk_percent": e.risk_percent, "reasons": e.reasons, "checks": e.checks, "at": e.created_at}
            for e in db.scalars(select(RiskEvaluation).order_by(RiskEvaluation.id.desc()).limit(min(limit, 500)))]


# ---- portfolio --------------------------------------------------------------------------------
@router.get("/portfolio")
def portfolio(_: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = tr.active_account(db)
    if not acct:
        return {}
    snap = db.scalar(select(tr.AccountSnapshot).where(tr.AccountSnapshot.account_id == acct.id)
                     .order_by(tr.AccountSnapshot.id.desc()).limit(1))
    eq = f(snap.equity) if snap else None
    longs = shorts = 0.0
    ccy: dict[str, float] = defaultdict(float)
    by_sym: dict[str, float] = defaultdict(float)
    risk_total = 0.0
    for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == acct.id)):
        r = tr.position_risk(db, p)
        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbol))
        sgn = 1 if p.direction == "BUY" else -1
        if sgn > 0:
            longs += r.notional
        else:
            shorts += r.notional
        by_sym[p.symbol] += r.notional
        risk_total += r.risk_amount
        if sym and sym.base_currency:
            ccy[sym.base_currency] += sgn * r.notional
        if sym and sym.profit_currency:
            ccy[sym.profit_currency] -= sgn * r.notional
    total = longs + shorts
    conc = {k: round(v / total, 4) for k, v in by_sym.items()} if total else {}
    warnings = [f"{k} is {v:.0%} of exposure" for k, v in conc.items() if v > 0.5 and len(conc) > 1]
    return {"equity": eq, "total_exposure": round(total, 2), "long_exposure": round(longs, 2),
            "short_exposure": round(shorts, 2), "net_currency_exposure": {k: round(v, 2) for k, v in ccy.items()},
            "symbol_concentration": conc, "open_risk": round(risk_total, 2),
            "open_risk_pct": round(risk_total / eq * 100, 3) if eq else None,
            "leverage": round(total / eq, 3) if eq else None,
            "margin_utilisation": round(f(snap.margin) / eq * 100, 2) if snap and eq else None, "warnings": warnings}


@router.get("/portfolio/equity")
def equity_curve(_: User = Depends(current_user), db: Session = Depends(get_db), days: int = 30):
    acct = tr.active_account(db)
    if not acct:
        return []
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(tr.AccountSnapshot.created_at, tr.AccountSnapshot.balance, tr.AccountSnapshot.equity)
                      .where(tr.AccountSnapshot.account_id == acct.id, tr.AccountSnapshot.created_at >= since)
                      .order_by(tr.AccountSnapshot.id)).all()
    step = max(1, len(rows) // 1000)
    return [{"t": r[0], "balance": f(r[1]), "equity": f(r[2])} for r in rows[::step]]


# ---- news -------------------------------------------------------------------------------------------
@router.get("/news")
def news_events(_: User = Depends(current_user), db: Session = Depends(get_db), hours: int = 48):
    now = datetime.now(UTC)
    from app.core.config import get_settings
    s = get_settings()
    evs = db.scalars(select(EconomicEvent).where(EconomicEvent.scheduled_at.between(now - timedelta(hours=6),
                                                                                    now + timedelta(hours=hours)))
                     .order_by(EconomicEvent.scheduled_at)).all()
    affected = defaultdict(list)
    for sym in db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True))):
        for c in news.symbol_currencies(sym, sym.broker_symbol):
            affected[c].append(sym.canonical)
    return {"provider_status": "CONNECTED" if news.provider_healthy() else "UNAVAILABLE",
            "note": None if news.provider_healthy() else "News risk is UNKNOWN; trading restricted by policy",
            "events": [{"id": e.id, "title": e.title, "currency": e.currency, "impact": e.impact,
                        "time": e.scheduled_at, "forecast": e.forecast, "previous": e.previous, "actual": e.actual,
                        "affected_symbols": sorted(set(affected.get(e.currency, [])))[:20],
                        "risk_window": [e.scheduled_at - timedelta(minutes=s.news_block_minutes_before),
                                        e.scheduled_at + timedelta(minutes=s.news_block_minutes_after)],
                        "restricted_now": news.is_high_impact(e) and
                        e.scheduled_at - timedelta(minutes=s.news_block_minutes_before) <= now <=
                        e.scheduled_at + timedelta(minutes=s.news_block_minutes_after)} for e in evs]}


@router.post("/news/refresh")
def news_refresh(_: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    return news.refresh(db)


# ---- notifications ------------------------------------------------------------------------------------
@router.get("/notifications")
def notifications(_: User = Depends(current_user), db: Session = Depends(get_db), unread: bool = False):
    q = select(Notification).order_by(Notification.id.desc()).limit(200)
    if unread:
        q = q.where(Notification.read.is_(False))
    return [{"id": n.id, "kind": n.kind, "severity": n.severity, "title": n.title, "body": n.body, "read": n.read,
             "channels": n.channels, "at": n.created_at} for n in db.scalars(q)]


@router.post("/notifications/read")
def mark_read(ids: list[int], _: User = Depends(current_user), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.id.in_(ids))):
        n.read = True
    return {"ok": True}


@router.get("/notifications/settings")
def notif_settings(_: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = {s.kind: s for s in db.scalars(select(NotificationSetting))}
    from app.notifications.service import CRITICAL
    return [{"kind": k, "in_app": rows[k].in_app if k in rows else True,
             "telegram": rows[k].telegram if k in rows else k in CRITICAL,
             "email": rows[k].email if k in rows else k in CRITICAL} for k in KINDS]


class NotifSetting(BaseModel):
    kind: str
    in_app: bool
    telegram: bool
    email: bool


@router.put("/notifications/settings")
def put_notif(body: list[NotifSetting], user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    for b in body:
        if b.kind not in KINDS:
            raise HTTPException(400, f"unknown kind {b.kind}")
        s = db.scalar(select(NotificationSetting).where(NotificationSetting.kind == b.kind)) or NotificationSetting(kind=b.kind)
        s.in_app, s.telegram, s.email = b.in_app, b.telegram, b.email
        db.add(s)
    audit(db, user.id, "NOTIFICATION_SETTINGS", "", {"count": len(body)})
    return {"ok": True}
