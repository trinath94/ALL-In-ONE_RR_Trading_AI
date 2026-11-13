"""Central strategy registry.

Production strategies are code-reviewed modules registered here. Experimental strategies
(e.g. Claude proposals) live in app.strategies.experimental and are *never* registered
into the production registry until promoted by an administrator.
"""
from __future__ import annotations

from functools import lru_cache

from app.strategies import (combinations, fibonacci, ict, indicators, liquidity, price_action, quantitative, reversal,
                            sessions, smc, trend, volume)
from app.strategies.base import Strategy, StrategyContext, StrategyMeta, StrategySignal

CATEGORY_MODULES = {
    "smc": smc, "ict": ict, "price_action": price_action, "liquidity": liquidity, "trend": trend,
    "indicators": indicators, "volume": volume, "sessions": sessions, "fibonacci": fibonacci,
    "reversal": reversal, "quantitative": quantitative, "combinations": combinations,
}

EXPECTED_COUNTS = {"smc": 20, "ict": 20, "price_action": 30, "liquidity": 20, "trend": 15, "indicators": 25,
                   "volume": 15, "sessions": 15, "fibonacci": 10, "reversal": 10, "quantitative": 20,
                   "combinations": 15}


@lru_cache
def all_strategies() -> dict[str, type[Strategy]]:
    out: dict[str, type[Strategy]] = {}
    for cat, mod in CATEGORY_MODULES.items():
        for cls in mod.STRATEGIES:
            if cls.code in out:
                raise RuntimeError(f"Duplicate strategy code {cls.code}")
            if cls.category != cat:
                raise RuntimeError(f"{cls.code} category {cls.category} != {cat}")
            out[cls.code] = cls
    return out


def get(code: str) -> type[Strategy]:
    return all_strategies()[code]


def metas() -> list[StrategyMeta]:
    return [cls.meta() for cls in all_strategies().values()]


def evaluate_all(ctx: StrategyContext, enabled: dict[str, dict] | None = None) -> tuple[list[StrategySignal], list[dict]]:
    """Run every enabled strategy on ctx. `enabled` maps code -> param overrides.
    Returns (signals, diagnostics). Exceptions in one strategy never stop the others."""
    signals: list[StrategySignal] = []
    diag: list[dict] = []
    for code, cls in all_strategies().items():
        if enabled is not None and code not in enabled:
            continue
        overrides = (enabled or {}).get(code) or {}
        strat = cls(**overrides)
        ok, why = strat.available(ctx)
        if not ok:
            diag.append({"strategy": code, "status": "unavailable", "reason": why})
            continue
        try:
            sig = strat.evaluate(ctx)
        except Exception as exc:  # isolate faulty strategies
            diag.append({"strategy": code, "status": "error", "reason": repr(exc)[:300]})
            continue
        if sig:
            signals.append(sig)
            diag.append({"strategy": code, "status": "signal", "direction": sig.direction})
        else:
            diag.append({"strategy": code, "status": "no_signal"})
    return signals, diag
