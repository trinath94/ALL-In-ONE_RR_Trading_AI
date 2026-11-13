"""Event-driven backtester with realistic fills.

Bias controls
-------------
* Look-ahead: strategies see bars [i-window, i] only; orders fill at bar i+1 open.
* Repainting: swing points are only usable after their confirmation bar (see structure.py).
* Higher timeframes: only HTF bars that have fully closed by bar i's close are visible.
* Fills: BUY at ask (= bid OHLC + spread), exits at bid; SELL mirrored. Slippage added
  against the trader on entries and stop exits. Gaps through a stop fill at the open.
* Same-bar ambiguity: if SL and TP are both inside one bar, the SL is assumed first.
* Costs: commission per lot per side, spread from the bar's recorded spread when available.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from app.analysis import regime as rg
from app.signals.engine import MarketContext, SignalConfig, TF_MINUTES, build_signal
from app.strategies import registry
from app.strategies.base import StrategyContext


@dataclass
class CostModel:
    spread_points: float | None = None     # None => use bar 'spread' column (points)
    commission_per_lot: float = 0.0        # per side, account currency
    slippage_points: float = 0.0


@dataclass
class InstrumentSpec:
    symbol: str = "SYMBOL"
    point: float = 0.00001
    tick_size: float = 0.00001
    tick_value: float = 1.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0


@dataclass
class BacktestConfig:
    strategies: list[str]
    timeframe: str = "H1"
    initial_balance: float = 10_000
    risk_pct: float = 1.0
    sizing: str = "risk"                   # risk | fixed
    fixed_lots: float = 0.1
    window: int = 300
    use_signal_engine: bool = False        # True => trade only engine BUY/SELL decisions (A/A+)
    min_grade: str = "A"
    max_bars_in_trade: int = 0             # 0 = unlimited
    htf: str | None = None                 # e.g. "H4" resampled from base data
    params: dict[str, dict] = field(default_factory=dict)
    costs: CostModel = field(default_factory=CostModel)
    spec: InstrumentSpec = field(default_factory=InstrumentSpec)


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    direction: str
    entry: float
    exit: float
    stop: float
    target: float
    volume: float
    pnl: float
    r: float
    reason: str
    strategy: str
    regime: str
    bars: int


RULES = {"H1": "1h", "H4": "4h", "D1": "1D", "W1": "W-SUN", "M30": "30min", "M15": "15min"}


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "spread" in df:
        agg["spread"] = "mean"
    return df.resample(RULES[tf], label="left", closed="left").agg(agg).dropna(subset=["open"])


def closed_htf(htf: pd.DataFrame, tf: str, base_tf: str, bar_time: pd.Timestamp) -> pd.DataFrame:
    """HTF bars whose period ended at or before the close of the base bar starting at bar_time."""
    base_close = bar_time + pd.Timedelta(minutes=TF_MINUTES[base_tf])
    ends = htf.index + pd.Timedelta(minutes=TF_MINUTES[tf])
    return htf[ends <= base_close]


def _floor(v: float, step: float) -> float:
    return math.floor(v / step + 1e-9) * step


def run_backtest(df: pd.DataFrame, cfg: BacktestConfig, start: int | None = None, end: int | None = None) -> dict:
    df = df.copy()
    if "volume" not in df and "tick_volume" in df:
        df["volume"] = df["tick_volume"]
    spec, costs = cfg.spec, cfg.costs
    strategies = {c: registry.get(c)(**cfg.params.get(c, {})) for c in cfg.strategies}
    htf_df = resample(df, cfg.htf) if cfg.htf else None
    start = max(start or 0, cfg.window)
    end = min(end or len(df), len(df))
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread_col = df["spread"].to_numpy(float) if "spread" in df else None
    idx = df.index

    def spread_at(i: int) -> float:
        pts = costs.spread_points if costs.spread_points is not None else (spread_col[i] if spread_col is not None else 0)
        return pts * spec.point

    balance = cfg.initial_balance
    equity_curve: list[tuple[str, float]] = []
    trades: list[Trade] = []
    pos = None
    grades = ["A+", "A", "B", "C"]
    min_rank = grades.index(cfg.min_grade) if cfg.min_grade in grades else 1

    i = start
    while i < end:
        # ---- manage open position on bar i (entered at a previous bar's next open) ----------
        if pos is not None:
            sgn = 1 if pos["dir"] == "BUY" else -1
            sp = spread_at(i)
            slip = costs.slippage_points * spec.point
            # Price path for exits: BUY exits on bid (OHLC), SELL exits on ask (OHLC + spread)
            bo, bh, bl = (o[i], h[i], l[i]) if sgn > 0 else (o[i] + sp, h[i] + sp, l[i] + sp)
            exit_px = reason = None
            if (bo - pos["sl"]) * sgn <= 0:                     # gapped through stop
                exit_px, reason = bo - sgn * slip, "SL_GAP"
            elif (bo - pos["tp"]) * sgn >= 0:                   # gapped through target (no positive slippage)
                exit_px, reason = pos["tp"], "TP_GAP"
            else:
                hit_sl = (bl <= pos["sl"]) if sgn > 0 else (bh >= pos["sl"])
                hit_tp = (bh >= pos["tp"]) if sgn > 0 else (bl <= pos["tp"])
                if hit_sl:                                       # pessimistic: SL before TP
                    exit_px, reason = pos["sl"] - sgn * slip, "SL"
                elif hit_tp:
                    exit_px, reason = pos["tp"], "TP"
            pos["bars"] += 1
            if exit_px is None and cfg.max_bars_in_trade and pos["bars"] >= cfg.max_bars_in_trade:
                exit_px, reason = (c[i] if sgn > 0 else c[i] + sp), "TIME"
            if exit_px is not None:
                pnl = (exit_px - pos["entry"]) * sgn / spec.tick_size * spec.tick_value * pos["vol"]
                pnl -= costs.commission_per_lot * pos["vol"] * 2
                risk_money = abs(pos["entry"] - pos["sl"]) / spec.tick_size * spec.tick_value * pos["vol"]
                balance += pnl
                trades.append(Trade(str(pos["t"]), str(idx[i]), pos["dir"], pos["entry"], exit_px, pos["sl"], pos["tp"],
                                    pos["vol"], round(pnl, 2), round(pnl / risk_money, 3) if risk_money else 0.0, reason,
                                    pos["strategy"], pos["regime"], pos["bars"]))
                pos = None
        equity_curve.append((str(idx[i]), round(balance, 2)))

        # ---- decision at close of bar i, fill at open of i+1 -------------------------------
        if pos is None and i < end - 1:
            win = df.iloc[i - cfg.window: i + 1]
            htf = {cfg.htf: closed_htf(htf_df, cfg.htf, cfg.timeframe, idx[i])} if htf_df is not None else {}
            ctx = StrategyContext(win, spec.symbol, cfg.timeframe, htf=htf, point=spec.point)
            sigs = []
            for code, s in strategies.items():
                try:
                    sig = s.evaluate(ctx)
                except Exception:  # noqa: BLE001 - a faulty strategy must not crash the test
                    sig = None
                if sig:
                    sigs.append(sig)
            chosen = None
            reg = None
            if sigs:
                reg = rg.classify(win)
                if cfg.use_signal_engine:
                    mctx = MarketContext(spec.symbol, cfg.timeframe, c[i], idx[i].to_pydatetime(), ctx.atr_last(),
                                         spread_at(i), reg.primary, reg.tags, ctx.htf_bias(), ["BACKTEST"], "CLEAR",
                                         {k: v.regimes for k, v in strategies.items()})
                    res = build_signal(sigs, mctx, SignalConfig())
                    if res.decision in ("BUY", "SELL") and grades.index(res.grade) <= min_rank:
                        chosen = (res.direction, res.stop_loss, res.tp2, "+".join(sorted({s["strategy"] for s in res.strategies})))
                else:
                    best = max(sigs, key=lambda s: s.strength)
                    chosen = (best.direction, best.stop_loss, best.take_profit, best.strategy)
            if chosen:
                d, sl, tp, strat = chosen
                sgn = 1 if d == "BUY" else -1
                sp = spread_at(i + 1)
                slip = costs.slippage_points * spec.point
                entry = o[i + 1] + (sp if sgn > 0 else 0) + sgn * slip
                if (entry - sl) * sgn > 0 and (tp - entry) * sgn > 0:
                    risk_per_lot = abs(entry - sl) / spec.tick_size * spec.tick_value
                    vol = cfg.fixed_lots if cfg.sizing == "fixed" else _floor(balance * cfg.risk_pct / 100 / risk_per_lot,
                                                                              spec.volume_step)
                    vol = min(vol, spec.volume_max)
                    if vol >= spec.volume_min and balance > 0:
                        pos = {"dir": d, "entry": entry, "sl": sl, "tp": tp, "vol": vol, "t": idx[i + 1], "bars": 0,
                               "strategy": strat, "regime": reg.primary if reg else ""}
        i += 1
    if pos is not None:  # mark-to-market close at the last bar
        sgn = 1 if pos["dir"] == "BUY" else -1
        exit_px = c[end - 1] if sgn > 0 else c[end - 1] + spread_at(end - 1)
        pnl = (exit_px - pos["entry"]) * sgn / spec.tick_size * spec.tick_value * pos["vol"] - costs.commission_per_lot * pos["vol"] * 2
        risk_money = abs(pos["entry"] - pos["sl"]) / spec.tick_size * spec.tick_value * pos["vol"]
        balance += pnl
        trades.append(Trade(str(pos["t"]), str(idx[end - 1]), pos["dir"], pos["entry"], exit_px, pos["sl"], pos["tp"],
                            pos["vol"], round(pnl, 2), round(pnl / risk_money, 3) if risk_money else 0, "END",
                            pos["strategy"], pos["regime"], pos["bars"]))
    if not equity_curve or equity_curve[-1][1] != round(balance, 2):
        equity_curve.append((str(idx[end - 1]), round(balance, 2)))
    return {"metrics": metrics(trades, equity_curve, cfg.initial_balance),
            "trades": [asdict(t) for t in trades], "equity_curve": equity_curve}


def metrics(trades: list[Trade], curve: list[tuple[str, float]], initial: float) -> dict:
    pnls = np.array([t.pnl for t in trades], float)
    rs = np.array([t.r for t in trades], float)
    eq = np.array([e for _, e in curve], float) if curve else np.array([initial])
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.max()) if len(dd) else 0.0
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    net = float(pnls.sum())

    def streak(cond):
        best = cur = 0
        for v in cond:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    ser = pd.Series(eq, index=pd.to_datetime([t for t, _ in curve], utc=True)) if curve else pd.Series(dtype=float)
    daily = ser.resample("1D").last().dropna().pct_change().dropna() if len(ser) > 1 else pd.Series(dtype=float)
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 2 and daily.std() > 0 else None
    downside = daily[daily < 0]
    sortino = float(daily.mean() / downside.std() * np.sqrt(252)) if len(downside) > 2 and downside.std() > 0 else None
    monthly = (ser.resample("ME").last().pct_change().dropna() * 100).round(2) if len(ser) > 1 else pd.Series(dtype=float)
    n = len(trades)
    return {
        "total_trades": n, "winning_trades": int(len(wins)), "losing_trades": int(len(losses)),
        "win_rate": round(len(wins) / n, 4) if n else None, "net_profit": round(net, 2),
        "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else (None if gp == 0 else float("inf")),
        "max_drawdown_pct": round(max_dd * 100, 2), "avg_r": round(float(rs.mean()), 3) if n else None,
        "expected_value": round(float(pnls.mean()), 2) if n else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "recovery_factor": round(net / (max_dd * initial), 3) if max_dd > 0 else None,
        "max_consecutive_wins": streak(pnls > 0), "max_consecutive_losses": streak(pnls <= 0),
        "monthly_returns": {k.strftime("%Y-%m"): float(v) for k, v in monthly.items()},
        "final_balance": round(float(eq[-1]), 2), "return_pct": round((float(eq[-1]) / initial - 1) * 100, 2),
        "sample_warning": "fewer than 30 trades: statistically inconclusive" if n < 30 else None,
    }


def split_oos(df: pd.DataFrame, cfg: BacktestConfig, oos_fraction: float = 0.3) -> dict:
    cut = int(len(df) * (1 - oos_fraction))
    return {"in_sample": run_backtest(df, cfg, end=cut), "out_of_sample": run_backtest(df, cfg, start=cut),
            "cut_time": str(df.index[cut])}


def monte_carlo(trades: list[dict], initial: float, runs: int = 1000, seed: int = 42) -> dict:
    """Bootstrap resampling of trade P&L sequence -> drawdown / outcome distribution."""
    if not trades:
        return {"runs": 0}
    rng = np.random.default_rng(seed)
    pnl = np.array([t["pnl"] for t in trades], float)
    finals, dds = [], []
    for _ in range(runs):
        seq = rng.choice(pnl, size=len(pnl), replace=True)
        eq = initial + np.cumsum(seq)
        peak = np.maximum.accumulate(np.r_[initial, eq])
        dds.append(float(((peak[1:] - eq) / peak[1:]).max()) * 100)
        finals.append(float(eq[-1]))
    pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
    return {"runs": runs, "final_balance": {q: pct(finals, q) for q in (5, 25, 50, 75, 95)},
            "max_drawdown_pct": {q: pct(dds, q) for q in (5, 25, 50, 75, 95)},
            "probability_of_loss": round(float(np.mean(np.array(finals) < initial)), 4)}


def spread_sensitivity(df: pd.DataFrame, cfg: BacktestConfig, base_points: float, multipliers=(1, 1.5, 2, 3)) -> list[dict]:
    out = []
    for m in multipliers:
        c2 = BacktestConfig(**{**cfg.__dict__, "costs": CostModel(base_points * m, cfg.costs.commission_per_lot,
                                                                   cfg.costs.slippage_points)})
        r = run_backtest(df, c2)["metrics"]
        out.append({"spread_points": base_points * m, "net_profit": r["net_profit"], "profit_factor": r["profit_factor"],
                    "max_drawdown_pct": r["max_drawdown_pct"], "trades": r["total_trades"]})
    return out


def _objective(m: dict, min_trades: int) -> float:
    if m["total_trades"] < min_trades or m["profit_factor"] in (None,):
        return -1e9
    pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 10.0
    return pf * (1 - m["max_drawdown_pct"] / 100)


def optimise(df: pd.DataFrame, cfg: BacktestConfig, code: str, grid: dict[str, list], oos_fraction: float = 0.3,
             min_trades: int = 20, top: int = 10) -> list[dict]:
    """Grid search ranked on in-sample objective; every candidate is reported with its OOS result so
    overfitting is visible (an IS winner that collapses OOS should be rejected)."""
    import itertools
    keys = list(grid)
    cut = int(len(df) * (1 - oos_fraction))
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = dict(zip(keys, combo))
        c2 = BacktestConfig(**{**cfg.__dict__, "strategies": [code], "params": {code: p}})
        ins = run_backtest(df, c2, end=cut)["metrics"]
        rows.append({"params": p, "in_sample": ins, "score": _objective(ins, min_trades)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    for rank, r in enumerate(rows[:top], 1):
        c2 = BacktestConfig(**{**cfg.__dict__, "strategies": [code], "params": {code: r["params"]}})
        r["out_of_sample"] = run_backtest(df, c2, start=cut)["metrics"]
        r["rank"] = rank
    return rows[:top]


def walk_forward(df: pd.DataFrame, cfg: BacktestConfig, code: str, grid: dict[str, list], n_splits: int = 4,
                 train_frac: float = 0.7, min_trades: int = 10) -> dict:
    """Rolling walk-forward: optimise on each train window, trade the next unseen window with the
    chosen parameters; concatenated OOS results are the honest performance estimate."""
    n = len(df)
    seg = n // n_splits
    windows, oos_trades = [], []
    for k in range(n_splits):
        a, b = k * seg, (k + 1) * seg if k < n_splits - 1 else n
        part = df.iloc[max(a - cfg.window, 0):b]
        best = optimise(part, cfg, code, grid, oos_fraction=1 - train_frac, min_trades=min_trades, top=1)
        if not best:
            continue
        w = best[0]
        windows.append({"window": k, "start": str(df.index[a]), "end": str(df.index[b - 1]), "params": w["params"],
                        "in_sample": w["in_sample"], "out_of_sample": w.get("out_of_sample")})
        c2 = BacktestConfig(**{**cfg.__dict__, "strategies": [code], "params": {code: w["params"]}})
        cut = int(len(part) * train_frac)
        oos_trades += run_backtest(part, c2, start=cut)["trades"]
    curve, bal = [], cfg.initial_balance
    for t in oos_trades:
        bal += t["pnl"]
        curve.append((t["exit_time"], bal))
    tr_objs = [Trade(**t) for t in oos_trades]
    return {"windows": windows, "combined_oos": metrics(tr_objs, curve, cfg.initial_balance)}
