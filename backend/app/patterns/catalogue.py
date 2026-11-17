"""Pattern catalogue (Part 28.1).

Derived at run time from the original strategies' own metadata (code, name, category, timeframes, data requirements),
so it can never disagree with them and never redefines them. A family is assigned from the strategy's category and
code; strategies that are not chart or structure patterns (indicator, trend, session, quantitative and combination
strategies) are listed with ``is_pattern: false`` for completeness. The new detectors in this package are added as
``source: "pattern_detector"``.
"""
from __future__ import annotations

CLASSICAL = ("head_shoulders", "inverse_head_shoulders", "double_top", "double_bottom", "triple_top", "triple_bottom",
             "triangle", "wedge", "channel", "flag", "range", "trendline")
CANDLESTICK = ("pin_bar", "engulfing", "inside_bar", "outside_bar", "fakey", "candlestick_reversal", "momentum_candle")
GAPS = ("smc_fvg", "smc_ifvg", "smc_bpr", "vol_volume_imbalance", "fib_fvg")
WYCKOFF = ("vol_wyckoff", "vol_wyckoff_spring", "vol_wyckoff_upthrust", "vol_accum_dist", "combo_wyckoff_vp")
VSA = ("vol_vsa", "vol_climax", "vol_absorption")
NON_PATTERN = ("indicators", "trend", "sessions", "quantitative", "combinations", "reversal")

DETECTORS = [
    {"code": "harmonic_gartley", "name": "Gartley", "family": "harmonic"},
    {"code": "harmonic_bat", "name": "Bat", "family": "harmonic"},
    {"code": "harmonic_butterfly", "name": "Butterfly", "family": "harmonic"},
    {"code": "harmonic_crab", "name": "Crab", "family": "harmonic"},
    {"code": "harmonic_abcd", "name": "AB=CD", "family": "harmonic"},
    {"code": "elliott_impulse", "name": "Elliott impulse (conservative count)", "family": "elliott"},
]


def family_of(code: str, category: str) -> tuple[str, bool]:
    if code in GAPS:
        return "gaps_imbalances", True
    if code in WYCKOFF:
        return "wyckoff", True
    if code in VSA:
        return "vsa", True
    if category == "price_action":
        if any(k in code for k in CANDLESTICK):
            return "candlestick", True
        if any(k in code for k in CLASSICAL):
            return "classical", True
        return "price_structure", True
    if category in ("smc", "ict", "liquidity", "fibonacci"):
        return category, True
    if category == "volume":
        return "order_flow", True
    return category, category not in NON_PATTERN


def catalogue() -> list[dict]:
    from app.strategies import registry
    out = []
    for m in registry.metas():
        fam, is_pattern = family_of(m.code, m.category)
        out.append({"code": m.code, "name": m.name, "family": fam, "is_pattern": is_pattern, "category": m.category,
                    "timeframes": m.timeframes, "requires": m.requires, "source": "original_strategy"})
    for d in DETECTORS:
        out.append({**d, "is_pattern": True, "category": "pattern_detector", "timeframes": ["M15", "H1", "H4", "D1"],
                    "requires": ["ohlc"], "source": "pattern_detector"})
    return out
