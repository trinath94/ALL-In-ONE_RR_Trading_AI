"""Sandbox runner — executed ONLY as a separate subprocess by ``sandbox.run_job``.

Reads a JSON job from stdin: {"task": "unit"|"validate"|"evaluate", "source": str, "bars": path|null,
"params": {...}, "costs": {...}} and writes a JSON result to stdout.

Isolation inside the subprocess (defence in depth, on top of the parent's static analysis):
* POSIX resource limits: CPU seconds, address space, open files, file size, no core dumps.
* The experimental source is exec'd with a restricted ``__builtins__`` and an import hook that only
  allows math/numpy/pandas/statistics. The sandbox API (Strategy, StrategySignal, candle, indicators,
  structure) is injected as plain names.
* The parent passes a scrubbed environment and a throwaway working directory.
"""
from __future__ import annotations

import builtins
import json
import resource
import sys
import traceback


def _limits(cpu: int, mem_mb: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


ALLOWED = {"math", "numpy", "pandas", "statistics"}
SAFE_BUILTINS = {k: getattr(builtins, k) for k in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len", "list", "map",
    "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip", "ValueError",
    "ZeroDivisionError", "IndexError", "KeyError", "Exception", "True", "False", "None", "print", "frozenset",
    "staticmethod", "classmethod", "property")
    if hasattr(builtins, k)}
SAFE_BUILTINS["__build_class__"] = builtins.__build_class__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if name.split(".")[0] not in ALLOWED or level:
        raise ImportError(f"import of '{name}' blocked by sandbox")
    return __import__(name, globals, locals, fromlist, level)


def load_strategy(src: str):
    import numpy as np
    import pandas as pd
    from app.analysis import indicators as ind
    from app.analysis import structure as st
    from app.strategies.base import Strategy, StrategySignal, candle
    b = dict(SAFE_BUILTINS)
    b["__import__"] = _guarded_import
    ns = {"__builtins__": b, "__name__": "experimental", "Strategy": Strategy, "StrategySignal": StrategySignal,
          "candle": candle, "ind": ind, "st": st, "np": np, "pd": pd}
    exec(compile(src, "<experimental>", "exec"), ns)  # noqa: S102 - restricted namespace inside the sandbox process
    classes = [v for v in ns.values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy]
    if len(classes) != 1:
        raise ValueError("expected exactly one Strategy subclass")
    return classes[0]


def unit_tests(cls) -> dict:
    """Behavioural tests on synthetic regimes: no exceptions, well-formed signals, determinism, no input mutation.
    (Look-ahead is prevented structurally: a strategy only ever receives bars up to the decision bar.)"""
    import numpy as np
    import pandas as pd
    from app.strategies.base import StrategyContext
    results, problems, fired = {}, [], 0

    def series(kind, n=700, seed=1):
        rng = np.random.default_rng(seed)
        drift = {"trend": 0.0004, "range": 0.0, "volatile": 0.0}[kind]
        vol = {"trend": 0.0008, "range": 0.0006, "volatile": 0.003}[kind]
        r = rng.normal(drift, vol, n)
        if kind == "range":
            r = r - 0.05 * np.cumsum(r) / np.arange(1, n + 1)
        c = 1.1 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, vol / 2, n)))
        lo = np.minimum(o, c) * (1 - abs(rng.normal(0, vol / 2, n)))
        idx = pd.date_range("2025-01-06", periods=n, freq="1h", tz="UTC")
        return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": rng.integers(100, 2000, n)}, index=idx)

    for kind in ("trend", "range", "volatile"):
        df = series(kind)
        s = cls()
        n_sig = 0
        for i in range(300, len(df), 2):
            ctx = StrategyContext(df.iloc[i - 300:i + 1])
            try:
                a = s.evaluate(ctx)
                b = s.evaluate(StrategyContext(df.iloc[i - 300:i + 1].copy()))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{kind}: exception at bar {i}: {exc!r}"[:300])
                break
            if (a is None) != (b is None) or (a and b and a.dict() != b.dict()):
                problems.append(f"{kind}: non-deterministic output at bar {i}")
                break
            if a is not None:
                n_sig += 1
                if not a.valid() or not np.isfinite([a.entry, a.stop_loss, a.take_profit]).all():
                    problems.append(f"{kind}: malformed signal at bar {i}")
                    break
            win = df.iloc[i - 300:i + 1]
            before = pd.util.hash_pandas_object(win).sum()
            s.evaluate(StrategyContext(win))
            if pd.util.hash_pandas_object(win).sum() != before:
                problems.append(f"{kind}: strategy mutates its input data")
                break
        results[kind] = n_sig
        fired += n_sig
    if fired == 0:
        problems.append("strategy produced no signals on any synthetic regime")
    return {"passed": not problems, "signals": results, "problems": problems}


def _bt(df, cls, costs, start=None, end=None, params=None):
    from app.backtesting import engine as bt
    from app.strategies import registry
    code = cls.code
    orig = registry.get

    def patched(c):
        return cls if c == code else orig(c)
    registry.get = patched
    try:
        cfg = bt.BacktestConfig([code], timeframe=costs.get("timeframe", "H1"), window=300, params={code: params or {}},
                                costs=bt.CostModel(costs.get("spread_points", 10), costs.get("commission_per_lot", 3.5),
                                                   costs.get("slippage_points", 1)),
                                spec=bt.InstrumentSpec(costs.get("symbol", "SYMBOL"), costs.get("point", 1e-5),
                                                       costs.get("tick_size", 1e-5), costs.get("tick_value", 1.0)))
        return bt.run_backtest(df, cfg, start=start, end=end)
    finally:
        registry.get = orig


def validate(cls, bars_path: str, costs: dict, n_trials: int, benchmark: str = "trend_ema_20_50") -> dict:
    """Development (60%) / validation (20%) / final test (20%) on identical assumptions.
    The final test is evaluated once and only if validation passes."""
    import numpy as np
    import pandas as pd
    from app.backtesting import engine as bt
    df = pd.read_parquet(bars_path) if bars_path.endswith(".parquet") else pd.read_pickle(bars_path)
    n = len(df)
    c1, c2 = int(n * 0.6), int(n * 0.8)
    rep: dict = {"bars": n, "split": {"development_end": str(df.index[c1 - 1]), "validation_end": str(df.index[c2 - 1])},
                 "n_trials_for_multiple_testing": n_trials}
    dev = _bt(df, cls, costs, end=c1)
    val = _bt(df, cls, costs, start=c1, end=c2)
    rep["development"] = dev["metrics"]
    rep["validation"] = val["metrics"]
    rep["monte_carlo_validation"] = bt.monte_carlo(val["trades"], 10_000, runs=500)
    rep["cost_sensitivity"] = []
    for mult in (1, 2, 3):
        r = _bt(df, cls, {**costs, "spread_points": costs.get("spread_points", 10) * mult}, start=c1, end=c2)["metrics"]
        rep["cost_sensitivity"].append({"spread_x": mult, "net": r["net_profit"], "pf": r["profit_factor"], "trades": r["total_trades"]})
    rep["slippage_sensitivity"] = []
    for sl in (0, 2, 5):
        r = _bt(df, cls, {**costs, "slippage_points": sl}, start=c1, end=c2)["metrics"]
        rep["slippage_sensitivity"].append({"slippage_points": sl, "net": r["net_profit"], "pf": r["profit_factor"]})
    # walk-forward over development+validation with fixed (declared) parameters, 4 folds
    folds = []
    seg = c2 // 4
    for k in range(4):
        a, b = max(k * seg, 300), (k + 1) * seg
        m = _bt(df, cls, costs, start=a, end=b)["metrics"]
        folds.append({"fold": k, "trades": m["total_trades"], "net": m["net_profit"], "pf": m["profit_factor"]})
    rep["walk_forward_folds"] = folds
    # benchmark on identical data & costs
    from app.strategies import registry
    bcls = registry.get(benchmark)
    rep["benchmark"] = {"strategy": benchmark, "validation": _bt(df, bcls, costs, start=c1, end=c2)["metrics"]}
    # significance with a Bonferroni-adjusted bound on validation R-multiples
    rs = np.array([t["r"] for t in val["trades"]], float)
    alpha = 0.05 / max(n_trials, 1)
    lo = None
    if len(rs) >= 10:
        rng = np.random.default_rng(3)
        boots = [rng.choice(rs, len(rs)).mean() for _ in range(2000)]
        lo = float(np.percentile(boots, 100 * alpha))
    vm = val["metrics"]
    checks = {
        "min_validation_trades": vm["total_trades"] >= 30,
        "validation_expectancy_significant": lo is not None and lo > 0,
        "survives_2x_spread": (rep["cost_sensitivity"][1]["net"] or 0) > 0,
        "max_drawdown_below_20pct": vm["max_drawdown_pct"] < 20,
        "walk_forward_majority_positive": sum(1 for f in folds if f["net"] > 0) >= 3,
        "monte_carlo_loss_probability_below_35pct": (rep["monte_carlo_validation"].get("probability_of_loss") or 1) < 0.35,
    }
    rep["expectancy_r_lower_bound"] = lo
    rep["checks"] = checks
    rep["validation_passed"] = all(checks.values())
    if rep["validation_passed"]:
        test = _bt(df, cls, costs, start=c2)
        rep["final_test"] = test["metrics"]
        rep["final_test_passed"] = test["metrics"]["total_trades"] >= 15 and (test["metrics"]["profit_factor"] or 0) > 1.0
    else:
        rep["final_test"] = None
        rep["final_test_passed"] = False
        rep["note"] = "Final test set left untouched because validation failed."
    return rep


def evaluate_bars(cls, bars_path: str) -> dict:
    """Shadow forward evaluation on the latest bars: does the strategy fire on the last closed bar?"""
    import pandas as pd
    from app.strategies.base import StrategyContext
    df = pd.read_pickle(bars_path)
    sig = cls().evaluate(StrategyContext(df.iloc[-301:]))
    return {"bar_time": str(df.index[-1]), "signal": sig.dict() if sig else None}


def main() -> None:
    job = json.loads(sys.stdin.read())
    _limits(int(job.get("cpu_seconds", 120)), int(job.get("memory_mb", 1536)))
    try:
        cls = load_strategy(job["source"])
        if job["task"] == "unit":
            out = unit_tests(cls)
        elif job["task"] == "validate":
            out = validate(cls, job["bars"], job.get("costs", {}), int(job.get("n_trials", 1)))
        elif job["task"] == "evaluate":
            out = evaluate_bars(cls, job["bars"])
        else:
            raise ValueError("unknown task")
        print(json.dumps({"ok": True, "result": out}, default=str))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": repr(exc)[:500], "trace": traceback.format_exc()[-2000:]}))


if __name__ == "__main__":
    main()
