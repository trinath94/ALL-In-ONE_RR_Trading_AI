"""Liquidity-based strategies (20)."""
from __future__ import annotations

import numpy as np

from app.analysis import indicators as ind
from app.analysis import sessions as ss
from app.analysis import structure as st
from app.strategies.base import C_LIQUIDITY, Strategy, StrategyContext, candle, last_swing

CAT = "liquidity"


class _DrawToEqual(Strategy):
    """Trend pullback entry targeting resting liquidity at equal highs/lows."""
    component, category = C_LIQUIDITY, CAT
    kind = "H"
    params = {"strength": 2, "tol_atr": 0.1}

    def detect(self, ctx):
        tol = self.p["tol_atr"] * ctx.atr_last()
        pairs = st.equal_levels(ctx.known_swings(self.p["strength"]), self.kind, tol)
        if not pairs:
            return None
        a, b = pairs[-1]
        c = candle(ctx)
        f = ctx.ema(20).iloc[-1]
        if self.kind == "H":
            lvl = max(a.price, b.price)
            if c["c"] < lvl and c["l"] <= f < c["c"] and c["bull"] and ctx.events()[1] > 0:
                return self.make(ctx, "BUY", c["l"], target=lvl, reasons=[f"Draw on EQH liquidity {lvl:.5f}"])
        else:
            lvl = min(a.price, b.price)
            if c["c"] > lvl and c["h"] >= f > c["c"] and c["bear"] and ctx.events()[1] < 0:
                return self.make(ctx, "SELL", c["h"], target=lvl, reasons=[f"Draw on EQL liquidity {lvl:.5f}"])
        return None


class EqualHighs(_DrawToEqual):
    code, name, kind = "liq_equal_highs", "Equal Highs", "H"


class EqualLows(_DrawToEqual):
    code, name, kind = "liq_equal_lows", "Equal Lows", "L"


class _PeriodSweep(Strategy):
    """Sweep of a previous period's high/low that closes back inside -> reversal."""
    component, category = C_LIQUIDITY, CAT
    rule, side = "D", "H"
    timeframes = ["M15", "H1", "H4"]

    def detect(self, ctx):
        lv = ctx.memo(("period", self.rule), lambda: st.period_levels(ctx.df, self.rule)).iloc[-1]
        c = candle(ctx)
        if self.side == "H":
            lvl = lv.get("high", np.nan)
            if not np.isnan(lvl) and c["h"] > lvl and c["c"] < lvl:
                return self.make(ctx, "SELL", c["h"], reasons=[f"{self.name} {lvl:.5f} swept and rejected"])
        else:
            lvl = lv.get("low", np.nan)
            if not np.isnan(lvl) and c["l"] < lvl and c["c"] > lvl:
                return self.make(ctx, "BUY", c["l"], reasons=[f"{self.name} {lvl:.5f} swept and rejected"])
        return None


class PDH(_PeriodSweep):
    code, name, rule, side = "liq_pdh", "Previous Day High (PDH)", "D", "H"


class PDL(_PeriodSweep):
    code, name, rule, side = "liq_pdl", "Previous Day Low (PDL)", "D", "L"


class PWH(_PeriodSweep):
    code, name, rule, side = "liq_pwh", "Previous Week High (PWH)", "W", "H"
    timeframes = ["H1", "H4"]


class PWL(_PeriodSweep):
    code, name, rule, side = "liq_pwl", "Previous Week Low (PWL)", "W", "L"
    timeframes = ["H1", "H4"]


class PMH(_PeriodSweep):
    code, name, rule, side = "liq_pmh", "Previous Month High (PMH)", "MS", "H"
    timeframes = ["H4", "D1"]
    min_bars = 200


class PML(_PeriodSweep):
    code, name, rule, side = "liq_pml", "Previous Month Low (PML)", "MS", "L"
    timeframes = ["H4", "D1"]
    min_bars = 200


class _SessionSweep(Strategy):
    """After a session closes, a sweep of its high or low that closes back inside reverses."""
    component, category = C_LIQUIDITY, CAT
    session = "ASIA"
    timeframes = ["M5", "M15", "M30"]
    min_bars = 150

    def available(self, ctx):
        if ctx.timeframe not in ("M1", "M5", "M15", "M30", "H1"):
            return False, "intraday timeframe required"
        return super().available(ctx)

    def detect(self, ctx):
        mask = ss.session_mask(ctx.df.index, self.session)
        if bool(mask.iloc[-1]):
            return None
        rng = ctx.memo(("sess", self.session), lambda: st.session_range(ctx.df, mask)).iloc[-1]
        if np.isnan(rng.high):
            return None
        c = candle(ctx)
        if c["h"] > rng.high and c["c"] < rng.high:
            return self.make(ctx, "SELL", c["h"], target=float(rng.low), reasons=[f"{self.session} high swept"])
        if c["l"] < rng.low and c["c"] > rng.low:
            return self.make(ctx, "BUY", c["l"], target=float(rng.high), reasons=[f"{self.session} low swept"])
        return None


class AsianSweep(_SessionSweep):
    code, name, session = "liq_asian_sweep", "Asian High and Low Sweep", "ASIA"


class LondonSweep(_SessionSweep):
    code, name, session = "liq_london_sweep", "London High and Low Sweep", "LONDON"


class NewYorkSweep(_SessionSweep):
    code, name, session = "liq_ny_sweep", "New York High and Low Sweep", "NEW_YORK"


class StopHunt(Strategy):
    """Spike > 0.5 ATR beyond a swing with a >= 60% wick, closing back inside."""
    code, name, category, component = "liq_stop_hunt", "Stop Hunt Reversal", CAT, C_LIQUIDITY
    params = {"strength": 3, "spike_atr": 0.5, "wick": 0.6}

    def detect(self, ctx):
        c, a = candle(ctx), ctx.atr_last()
        sh, sl = last_swing(ctx, "H", self.p["strength"]), last_swing(ctx, "L", self.p["strength"])
        if sh and c["h"] - sh.price >= self.p["spike_atr"] * a and c["c"] < sh.price and c["upper"] / c["range"] >= self.p["wick"]:
            return self.make(ctx, "SELL", c["h"], strength=0.65, reasons=["Stop hunt above swing high"])
        if sl and sl.price - c["l"] >= self.p["spike_atr"] * a and c["c"] > sl.price and c["lower"] / c["range"] >= self.p["wick"]:
            return self.make(ctx, "BUY", c["l"], strength=0.65, reasons=["Stop hunt below swing low"])
        return None


class LiquidityGrab(Strategy):
    """Two-bar grab: bar 1 closes beyond a swing, bar 2 closes back and engulfs it."""
    code, name, category, component = "liq_grab", "Liquidity Grab", CAT, C_LIQUIDITY
    params = {"strength": 3}

    def detect(self, ctx):
        b1, b2 = candle(ctx, 2), candle(ctx)
        sh = last_swing(ctx, "H", self.p["strength"], before=ctx.n - 2)
        sl = last_swing(ctx, "L", self.p["strength"], before=ctx.n - 2)
        if sh and b1["c"] > sh.price and b2["c"] < sh.price and b2["c"] < b1["o"]:
            return self.make(ctx, "SELL", max(b1["h"], b2["h"]), reasons=["Failed close above swing high"])
        if sl and b1["c"] < sl.price and b2["c"] > sl.price and b2["c"] > b1["o"]:
            return self.make(ctx, "BUY", min(b1["l"], b2["l"]), reasons=["Failed close below swing low"])
        return None


class LiquidityPool(Strategy):
    """3+ swing extremes clustered in a zone act as a magnet; trend entry targeting the pool."""
    code, name, category, component = "liq_pool", "Liquidity Pool Trading", CAT, C_LIQUIDITY
    params = {"strength": 2, "tol_atr": 0.5, "min_pts": 3}

    def detect(self, ctx):
        tol, c = self.p["tol_atr"] * ctx.atr_last(), candle(ctx)
        sw = ctx.known_swings(self.p["strength"])
        trend = ctx.events()[1]
        kind = "H" if trend > 0 else "L"
        prices = sorted(s.price for s in sw if s.kind == kind and ((s.price > c["c"]) if kind == "H" else (s.price < c["c"])))
        best = None
        for p in prices:
            group = [q for q in prices if abs(q - p) <= tol]
            if len(group) >= self.p["min_pts"]:
                best = float(np.mean(group)) if best is None or abs(np.mean(group) - c["c"]) < abs(best - c["c"]) else best
        if best is None:
            return None
        if trend > 0 and c["bull"] and c["c"] > ctx.ema(20).iloc[-1]:
            return self.make(ctx, "BUY", None, target=best, reasons=[f"Buy-side pool at {best:.5f}"])
        if trend < 0 and c["bear"] and c["c"] < ctx.ema(20).iloc[-1]:
            return self.make(ctx, "SELL", None, target=best, reasons=[f"Sell-side pool at {best:.5f}"])
        return None


class TrendlineLiquidity(Strategy):
    """Run through a 3-touch trendline (stops beneath it) followed by a reclaim."""
    code, name, category, component = "liq_trendline", "Trendline Liquidity", CAT, C_LIQUIDITY
    params = {"strength": 2}

    def detect(self, ctx):
        i, c, prev = ctx.n - 1, candle(ctx), candle(ctx, 2)
        lows = [s for s in ctx.known_swings(self.p["strength"]) if s.kind == "L"][-3:]
        if len(lows) == 3 and lows[0].price < lows[1].price < lows[2].price:
            y = lows[1].price + (lows[2].price - lows[1].price) * (i - lows[1].idx) / (lows[2].idx - lows[1].idx)
            if prev["c"] < y and c["c"] > y:
                return self.make(ctx, "BUY", min(prev["l"], c["l"]), reasons=["Trendline stops run then reclaimed"])
        highs = [s for s in ctx.known_swings(self.p["strength"]) if s.kind == "H"][-3:]
        if len(highs) == 3 and highs[0].price > highs[1].price > highs[2].price:
            y = highs[1].price + (highs[2].price - highs[1].price) * (i - highs[1].idx) / (highs[2].idx - highs[1].idx)
            if prev["c"] > y and c["c"] < y:
                return self.make(ctx, "SELL", max(prev["h"], c["h"]), reasons=["Trendline stops run then lost"])
        return None


class SwingHighLiquidity(Strategy):
    """Sweep of a major (strength 5) swing high, closing back below."""
    code, name, category, component = "liq_swing_high", "Swing High Liquidity", CAT, C_LIQUIDITY
    params = {"strength": 5}

    def detect(self, ctx):
        s, c = last_swing(ctx, "H", self.p["strength"]), candle(ctx)
        if s and c["h"] > s.price > c["c"]:
            return self.make(ctx, "SELL", c["h"], reasons=[f"Major swing high {s.price:.5f} liquidity taken"])
        return None


class SwingLowLiquidity(Strategy):
    """Sweep of a major (strength 5) swing low, closing back above."""
    code, name, category, component = "liq_swing_low", "Swing Low Liquidity", CAT, C_LIQUIDITY
    params = {"strength": 5}

    def detect(self, ctx):
        s, c = last_swing(ctx, "L", self.p["strength"]), candle(ctx)
        if s and c["l"] < s.price < c["c"]:
            return self.make(ctx, "BUY", c["l"], reasons=[f"Major swing low {s.price:.5f} liquidity taken"])
        return None


class RangeLiquiditySweep(Strategy):
    """In a low-ADX range, sweep of the 50-bar range edge, fade to the midline."""
    code, name, category, component = "liq_range_sweep", "Range Liquidity Sweep", CAT, C_LIQUIDITY
    params = {"n": 50, "adx_max": 22}
    regimes = ["SIDEWAYS", "CONSOLIDATION"]

    def detect(self, ctx):
        if ctx.adx().adx.iloc[-1] > self.p["adx_max"]:
            return None
        d, c = ind.donchian(ctx.df, self.p["n"]).iloc[-1], candle(ctx)
        if np.isnan(d.upper):
            return None
        if c["h"] > d.upper and c["c"] < d.upper:
            return self.make(ctx, "SELL", c["h"], target=float(d.mid), reasons=["Range high sweep"])
        if c["l"] < d.lower and c["c"] > d.lower:
            return self.make(ctx, "BUY", c["l"], target=float(d.mid), reasons=["Range low sweep"])
        return None


class InternalLiquiditySweep(Strategy):
    """Sweep of an internal (strength 1) swing against the trend, then trend continuation."""
    code, name, category, component = "liq_internal_sweep", "Internal Liquidity Sweep", CAT, C_LIQUIDITY

    def detect(self, ctx):
        trend, c = ctx.events()[1], candle(ctx)
        if trend > 0:
            s = last_swing(ctx, "L", 1, ctx.n - 1)
            if s and c["l"] < s.price < c["c"] and c["bull"]:
                return self.make(ctx, "BUY", c["l"], reasons=["Internal SSL swept in uptrend"])
        if trend < 0:
            s = last_swing(ctx, "H", 1, ctx.n - 1)
            if s and c["h"] > s.price > c["c"] and c["bear"]:
                return self.make(ctx, "SELL", c["h"], reasons=["Internal BSL swept in downtrend"])
        return None


class ExternalLiquiditySweep(Strategy):
    """Sweep of the 100-bar dealing-range extreme (external liquidity), reversal."""
    code, name, category, component = "liq_external_sweep", "External Liquidity Sweep", CAT, C_LIQUIDITY
    params = {"lookback": 100}
    min_bars = 110

    def detect(self, ctx):
        prev, c = ctx.df.iloc[-self.p["lookback"] - 1:-1], candle(ctx)
        hi, lo = float(prev.high.max()), float(prev.low.min())
        if c["h"] > hi > c["c"]:
            return self.make(ctx, "SELL", c["h"], target=(hi + lo) / 2, strength=0.6, reasons=["External high swept"])
        if c["l"] < lo < c["c"]:
            return self.make(ctx, "BUY", c["l"], target=(hi + lo) / 2, strength=0.6, reasons=["External low swept"])
        return None


STRATEGIES = [EqualHighs, EqualLows, PDH, PDL, PWH, PWL, PMH, PML, AsianSweep, LondonSweep, NewYorkSweep, StopHunt,
              LiquidityGrab, LiquidityPool, TrendlineLiquidity, SwingHighLiquidity, SwingLowLiquidity,
              RangeLiquiditySweep, InternalLiquiditySweep, ExternalLiquiditySweep]
_ = StrategyContext
