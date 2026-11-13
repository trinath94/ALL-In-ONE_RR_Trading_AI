"""Smart Money Concepts strategies (20)."""
from __future__ import annotations

import numpy as np

from app.analysis import structure as st
from app.strategies.base import (C_LIQUIDITY, C_SMC_ICT, C_STRUCTURE, Strategy, StrategyContext, candle,
                                 last_swing)

CAT = "smc"


def _rejects_into(ctx: StrategyContext, z_top: float, z_bot: float, direction: str) -> bool:
    c = candle(ctx)
    if direction == "BUY":
        return c["l"] <= z_top and c["c"] > z_top - 0.5 * (z_top - z_bot) and c["c"] > c["o"]
    return c["h"] >= z_bot and c["c"] < z_bot + 0.5 * (z_top - z_bot) and c["c"] < c["o"]


def _trend(ctx: StrategyContext) -> int:
    return ctx.events()[1]


class OrderBlock(Strategy):
    """Unmitigated order block revisited for the first time with a rejection candle."""
    code, name, category, component = "smc_order_block", "Order Block (OB)", CAT, C_SMC_ICT
    params = {"strength": 3, "max_age": 80, "rr": 2.5}
    regimes = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR"]

    def detect(self, ctx):
        i = ctx.n - 1
        for z in reversed(ctx.order_blocks(self.p["strength"])):
            if i - z.idx > self.p["max_age"] or z.idx >= i:
                continue
            if z.mitigated_idx != i or (z.broken_idx is not None and z.broken_idx <= i):
                continue
            if _rejects_into(ctx, z.top, z.bottom, z.direction):
                stop = z.bottom if z.direction == "BUY" else z.top
                return self.make(ctx, z.direction, stop, strength=0.6 + (0.1 if z.meta.get("event") == "BOS" else 0),
                                 reasons=[f"First return to {z.direction} OB {z.bottom:.5f}-{z.top:.5f}"])
        return None


class BreakerBlock(Strategy):
    """Failed order block (closed through) retested from the other side."""
    code, name, category, component = "smc_breaker_block", "Breaker Block", CAT, C_SMC_ICT
    params = {"strength": 3, "max_age": 60, "rr": 2.0}

    def detect(self, ctx):
        i = ctx.n - 1
        c = candle(ctx)
        for z in reversed(ctx.order_blocks(self.p["strength"])):
            if z.broken_idx is None or z.broken_idx >= i or i - z.broken_idx > self.p["max_age"]:
                continue
            if z.direction == "BUY" and c["h"] >= z.bottom and c["c"] < z.bottom and c["bear"]:
                return self.make(ctx, "SELL", z.top, reasons=["Bullish OB failed -> bearish breaker retest"])
            if z.direction == "SELL" and c["l"] <= z.top and c["c"] > z.top and c["bull"]:
                return self.make(ctx, "BUY", z.bottom, reasons=["Bearish OB failed -> bullish breaker retest"])
        return None


class MitigationBlock(Strategy):
    """Order block formed from a failure swing (lower high / higher low, no sweep) after CHOCH."""
    code, name, category, component = "smc_mitigation_block", "Mitigation Block", CAT, C_SMC_ICT
    params = {"strength": 3, "max_age": 60, "rr": 2.0}

    def detect(self, ctx):
        i = ctx.n - 1
        sw = ctx.known_swings(self.p["strength"])
        for z in reversed(ctx.order_blocks(self.p["strength"])):
            if z.meta.get("event") != "CHOCH" or z.mitigated_idx != i or i - z.idx > self.p["max_age"]:
                continue
            kind = "H" if z.direction == "SELL" else "L"
            prior = [s for s in sw if s.kind == kind and s.idx < z.meta["candle"]]
            if len(prior) < 2:
                continue
            a, b = prior[-2], prior[-1]
            failure = (b.price < a.price) if kind == "H" else (b.price > a.price)
            if failure and _rejects_into(ctx, z.top, z.bottom, z.direction):
                stop = z.bottom if z.direction == "BUY" else z.top
                return self.make(ctx, z.direction, stop, reasons=["Failure swing mitigation block retest"])
        return None


class RejectionBlock(Strategy):
    """Long-wick candle at a swing extreme; price re-enters the wick and rejects."""
    code, name, category, component = "smc_rejection_block", "Rejection Block", CAT, C_SMC_ICT
    params = {"strength": 3, "wick_ratio": 0.5, "max_age": 50, "rr": 2.0}

    def detect(self, ctx):
        df, i = ctx.df, ctx.n - 1
        o, c, h, l = df.open.values, df.close.values, df.high.values, df.low.values
        cur = candle(ctx)
        for s in reversed(ctx.known_swings(self.p["strength"])):
            if i - s.idx > self.p["max_age"] or s.idx >= i - 1:
                continue
            k = s.idx
            rng = max(h[k] - l[k], 1e-12)
            if s.kind == "H":
                body_top = max(o[k], c[k])
                if (h[k] - body_top) / rng >= self.p["wick_ratio"] and cur["h"] >= body_top and cur["h"] <= h[k] \
                        and cur["c"] < body_top and cur["bear"]:
                    return self.make(ctx, "SELL", h[k], reasons=["Return into swing-high rejection wick"])
            else:
                body_bot = min(o[k], c[k])
                if (body_bot - l[k]) / rng >= self.p["wick_ratio"] and cur["l"] <= body_bot and cur["l"] >= l[k] \
                        and cur["c"] > body_bot and cur["bull"]:
                    return self.make(ctx, "BUY", l[k], reasons=["Return into swing-low rejection wick"])
            break
        return None


class FairValueGap(Strategy):
    """Retracement into an unfilled FVG aligned with structure trend."""
    code, name, category, component = "smc_fvg", "Fair Value Gap (FVG)", CAT, C_SMC_ICT
    params = {"min_atr_frac": 0.2, "max_age": 40, "rr": 2.0}
    regimes = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "BREAKOUT"]

    def detect(self, ctx):
        i, trend = ctx.n - 1, _trend(ctx)
        cur = candle(ctx)
        for g in reversed(ctx.fvgs(self.p["min_atr_frac"])):
            if g.idx >= i or i - g.idx > self.p["max_age"]:
                continue
            if g.inverted_idx is not None and g.inverted_idx <= i:
                continue
            if g.direction == "BUY" and trend >= 0 and cur["l"] <= g.top and cur["c"] > g.mid and cur["bull"]:
                if g.filled_idx is not None and g.filled_idx < i:
                    continue
                return self.make(ctx, "BUY", g.bottom, reasons=[f"Bullish FVG {g.bottom:.5f}-{g.top:.5f} respected"])
            if g.direction == "SELL" and trend <= 0 and cur["h"] >= g.bottom and cur["c"] < g.mid and cur["bear"]:
                if g.filled_idx is not None and g.filled_idx < i:
                    continue
                return self.make(ctx, "SELL", g.top, reasons=[f"Bearish FVG {g.bottom:.5f}-{g.top:.5f} respected"])
        return None


class InverseFVG(Strategy):
    """FVG closed through (inverted) and retested from the opposite side."""
    code, name, category, component = "smc_ifvg", "Inverse Fair Value Gap (IFVG)", CAT, C_SMC_ICT
    params = {"min_atr_frac": 0.2, "max_age": 30, "rr": 2.0}

    def detect(self, ctx):
        i, cur = ctx.n - 1, candle(ctx)
        for g in reversed(ctx.fvgs(self.p["min_atr_frac"])):
            if g.inverted_idx is None or g.inverted_idx >= i or i - g.inverted_idx > self.p["max_age"]:
                continue
            if g.direction == "BUY" and cur["h"] >= g.bottom and cur["c"] < g.bottom and cur["bear"]:
                return self.make(ctx, "SELL", g.top, reasons=["Bullish FVG inverted -> resistance retest"])
            if g.direction == "SELL" and cur["l"] <= g.top and cur["c"] > g.top and cur["bull"]:
                return self.make(ctx, "BUY", g.bottom, reasons=["Bearish FVG inverted -> support retest"])
        return None


class BalancedPriceRange(Strategy):
    """Overlap of opposing FVGs; trade in direction of the more recent gap on re-entry."""
    code, name, category, component = "smc_bpr", "Balanced Price Range (BPR)", CAT, C_SMC_ICT
    params = {"min_atr_frac": 0.1, "max_age": 40, "rr": 2.0}

    def detect(self, ctx):
        i, cur = ctx.n - 1, candle(ctx)
        gaps = [g for g in ctx.fvgs(self.p["min_atr_frac"]) if i - g.idx <= self.p["max_age"] and g.idx < i]
        for a_i in range(len(gaps) - 1, 0, -1):
            b = gaps[a_i]
            for a in gaps[:a_i]:
                if a.direction == b.direction:
                    continue
                top, bot = min(a.top, b.top), max(a.bottom, b.bottom)
                if top <= bot:
                    continue
                if b.direction == "BUY" and cur["l"] <= top and cur["c"] > top and cur["bull"]:
                    return self.make(ctx, "BUY", bot, reasons=[f"BPR {bot:.5f}-{top:.5f} support"])
                if b.direction == "SELL" and cur["h"] >= bot and cur["c"] < bot and cur["bear"]:
                    return self.make(ctx, "SELL", top, reasons=[f"BPR {bot:.5f}-{top:.5f} resistance"])
        return None


class LiquiditySweep(Strategy):
    """Wick through the most recent swing high/low and close back inside."""
    code, name, category, component = "smc_liquidity_sweep", "Liquidity Sweep", CAT, C_LIQUIDITY
    params = {"strength": 3, "rr": 2.0}

    def detect(self, ctx):
        cur = candle(ctx)
        sh, sl = last_swing(ctx, "H", self.p["strength"]), last_swing(ctx, "L", self.p["strength"])
        if sh and cur["h"] > sh.price and cur["c"] < sh.price:
            return self.make(ctx, "SELL", cur["h"], strength=0.55, reasons=[f"Swept swing high {sh.price:.5f}"])
        if sl and cur["l"] < sl.price and cur["c"] > sl.price:
            return self.make(ctx, "BUY", cur["l"], strength=0.55, reasons=[f"Swept swing low {sl.price:.5f}"])
        return None


class _EqualSweep(Strategy):
    kind = "H"
    params = {"strength": 2, "tol_atr": 0.1, "rr": 2.0}

    def detect(self, ctx):
        tol = self.p["tol_atr"] * ctx.atr_last()
        cur = candle(ctx)
        pairs = st.equal_levels(ctx.known_swings(self.p["strength"]), self.kind, tol)
        if not pairs:
            return None
        a, b = pairs[-1]
        lvl = max(a.price, b.price) if self.kind == "H" else min(a.price, b.price)
        if self.kind == "H" and cur["h"] > lvl and cur["c"] < lvl:
            return self.make(ctx, "SELL", cur["h"], strength=0.65, reasons=[f"Buy-side liquidity (EQH {lvl:.5f}) run"])
        if self.kind == "L" and cur["l"] < lvl and cur["c"] > lvl:
            return self.make(ctx, "BUY", cur["l"], strength=0.65, reasons=[f"Sell-side liquidity (EQL {lvl:.5f}) run"])
        return None


class BuySideLiquidity(_EqualSweep):
    """Run on resting buy stops above equal highs followed by rejection."""
    code, name, category, component, kind = "smc_bsl", "Buy-Side Liquidity (BSL)", CAT, C_LIQUIDITY, "H"


class SellSideLiquidity(_EqualSweep):
    """Run on resting sell stops below equal lows followed by rejection."""
    code, name, category, component, kind = "smc_ssl", "Sell-Side Liquidity (SSL)", CAT, C_LIQUIDITY, "L"


class Inducement(Strategy):
    """Minor internal swing taken out in the trend's pullback while the major swing holds."""
    code, name, category, component = "smc_inducement", "Inducement", CAT, C_SMC_ICT
    params = {"major": 5, "minor": 1, "rr": 2.5}

    def detect(self, ctx):
        trend = _trend(ctx)
        cur = candle(ctx)
        if trend > 0:
            minor, major = last_swing(ctx, "L", self.p["minor"], ctx.n - 1), last_swing(ctx, "L", self.p["major"])
            if minor and major and minor.price > major.price and cur["l"] < minor.price < cur["c"] \
                    and cur["l"] > major.price:
                return self.make(ctx, "BUY", cur["l"], reasons=["Internal low inducement swept above major low"])
        if trend < 0:
            minor, major = last_swing(ctx, "H", self.p["minor"], ctx.n - 1), last_swing(ctx, "H", self.p["major"])
            if minor and major and minor.price < major.price and cur["h"] > minor.price > cur["c"] \
                    and cur["h"] < major.price:
                return self.make(ctx, "SELL", cur["h"], reasons=["Internal high inducement swept below major high"])
        return None


def _event_now(ctx, kind, strength=3):
    ev, _ = ctx.events(strength)
    return ev[-1] if ev and ev[-1].idx == ctx.n - 1 and (kind is None or ev[-1].kind == kind) else None


class ChangeOfCharacter(Strategy):
    """First break of structure against the prevailing trend."""
    code, name, category, component = "smc_choch", "Change of Character (CHOCH)", CAT, C_STRUCTURE
    params = {"strength": 3, "rr": 2.0}

    def detect(self, ctx):
        ev = _event_now(ctx, "CHOCH", self.p["strength"])
        if not ev:
            return None
        stop = float(ctx.df["low"].iloc[ev.swing_idx:].min()) if ev.direction == "BUY" else \
            float(ctx.df["high"].iloc[ev.swing_idx:].max())
        return self.make(ctx, ev.direction, stop, strength=0.5, reasons=[f"CHOCH through {ev.level:.5f}"])


class BreakOfStructure(Strategy):
    """Trend-continuation break of the last swing, confirmed by a strong body close."""
    code, name, category, component = "smc_bos", "Break of Structure (BOS)", CAT, C_STRUCTURE
    params = {"strength": 3, "body_frac": 0.5, "rr": 2.0}
    regimes = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "BREAKOUT"]

    def detect(self, ctx):
        ev = _event_now(ctx, "BOS", self.p["strength"])
        c = candle(ctx)
        if not ev or c["body"] / c["range"] < self.p["body_frac"]:
            return None
        opp = last_swing(ctx, "L" if ev.direction == "BUY" else "H", self.p["strength"])
        return self.make(ctx, ev.direction, opp.price if opp else None, reasons=[f"BOS through {ev.level:.5f}"])


class MarketStructureShift(Strategy):
    """Liquidity sweep followed (within N bars) by a displacement CHOCH."""
    code, name, category, component = "smc_mss", "Market Structure Shift (MSS)", CAT, C_STRUCTURE
    params = {"strength": 2, "window": 10, "rr": 2.5}

    def detect(self, ctx):
        ev = _event_now(ctx, "CHOCH", self.p["strength"])
        if not ev:
            return None
        disp = st.displacement(ctx.df.iloc[-self.p["window"]:]).values
        want = 1 if ev.direction == "BUY" else -1
        if not (disp == want).any():
            return None
        w = ctx.df.iloc[-self.p["window"]:]
        prior = ctx.df.iloc[-self.p["window"] * 4:-self.p["window"]]
        if ev.direction == "BUY" and w.low.min() < prior.low.min():
            return self.make(ctx, "BUY", float(w.low.min()), strength=0.7, reasons=["SSL sweep + displacement MSS"])
        if ev.direction == "SELL" and w.high.max() > prior.high.max():
            return self.make(ctx, "SELL", float(w.high.max()), strength=0.7, reasons=["BSL sweep + displacement MSS"])
        return None


class Displacement(Strategy):
    """Displacement candle breaking a 20-bar range — momentum continuation."""
    code, name, category, component = "smc_displacement", "Displacement", CAT, C_SMC_ICT
    params = {"mult": 2.0, "lookback": 20, "rr": 1.5, "sl_atr": 1.2}
    regimes = ["BREAKOUT", "STRONG_BULL", "STRONG_BEAR"]

    def detect(self, ctx):
        d = st.displacement(ctx.df.iloc[-60:], self.p["mult"]).iloc[-1]
        c = candle(ctx)
        prev = ctx.df.iloc[-self.p["lookback"] - 1:-1]
        if d > 0 and c["c"] > prev.high.max():
            return self.make(ctx, "BUY", c["l"], reasons=["Bullish displacement range break"])
        if d < 0 and c["c"] < prev.low.min():
            return self.make(ctx, "SELL", c["h"], reasons=["Bearish displacement range break"])
        return None


class PremiumDiscount(Strategy):
    """Buy in discount of bullish dealing range / sell in premium of bearish range."""
    code, name, category, component = "smc_premium_discount", "Premium and Discount Zones", CAT, C_SMC_ICT
    params = {"strength": 3, "lookback": 100, "threshold": 0.3, "rr": 2.0}

    def detect(self, ctx):
        rng = st.dealing_range(ctx.df, ctx.n - 1, self.p["strength"], self.p["lookback"])
        if not rng:
            return None
        trend, c = _trend(ctx), candle(ctx)
        pos = st.premium_discount(c["c"], rng)
        if trend > 0 and pos <= self.p["threshold"] and c["bull"]:
            return self.make(ctx, "BUY", rng[1], target=rng[0], reasons=[f"Discount {pos:.0%} of range in uptrend"])
        if trend < 0 and pos >= 1 - self.p["threshold"] and c["bear"]:
            return self.make(ctx, "SELL", rng[0], target=rng[1], reasons=[f"Premium {pos:.0%} of range in downtrend"])
        return None


class Equilibrium(Strategy):
    """Rejection at the 50% equilibrium of the dealing range in trend direction."""
    code, name, category, component = "smc_equilibrium", "Equilibrium Trading", CAT, C_SMC_ICT
    params = {"strength": 3, "lookback": 100, "tol": 0.05, "rr": 2.0}

    def detect(self, ctx):
        rng = st.dealing_range(ctx.df, ctx.n - 1, self.p["strength"], self.p["lookback"])
        if not rng:
            return None
        hi, lo = rng
        eq = (hi + lo) / 2
        tol = self.p["tol"] * (hi - lo)
        trend, c = _trend(ctx), candle(ctx)
        if trend > 0 and c["l"] <= eq + tol and c["c"] > eq and c["bull"]:
            return self.make(ctx, "BUY", lo + 0.25 * (hi - lo), target=hi, reasons=["Equilibrium hold in uptrend"])
        if trend < 0 and c["h"] >= eq - tol and c["c"] < eq and c["bear"]:
            return self.make(ctx, "SELL", hi - 0.25 * (hi - lo), target=lo, reasons=["Equilibrium reject in downtrend"])
        return None


class InternalRangeLiquidity(Strategy):
    """After external liquidity is taken, target the nearest internal liquidity (open FVG)."""
    code, name, category, component = "smc_irl", "Internal Range Liquidity", CAT, C_LIQUIDITY
    params = {"strength": 3, "lookback": 100}

    def detect(self, ctx):
        i, c = ctx.n - 1, candle(ctx)
        prior = ctx.df.iloc[-self.p["lookback"]:-1]
        open_gaps = [g for g in ctx.fvgs() if g.filled_idx is None and g.idx < i]
        if c["h"] > prior.high.max() and c["c"] < prior.high.max():
            below = [g for g in open_gaps if g.direction == "BUY" and g.top < c["c"]]
            if below:
                tgt = max(below, key=lambda g: g.top).mid
                return self.make(ctx, "SELL", c["h"], target=tgt, reasons=["ERL high taken -> draw to IRL (FVG)"])
        if c["l"] < prior.low.min() and c["c"] > prior.low.min():
            above = [g for g in open_gaps if g.direction == "SELL" and g.bottom > c["c"]]
            if above:
                tgt = min(above, key=lambda g: g.bottom).mid
                return self.make(ctx, "BUY", c["l"], target=tgt, reasons=["ERL low taken -> draw to IRL (FVG)"])
        return None


class ExternalRangeLiquidity(Strategy):
    """After internal liquidity (FVG) is rebalanced, target the external range extreme."""
    code, name, category, component = "smc_erl", "External Range Liquidity", CAT, C_LIQUIDITY
    params = {"strength": 3, "lookback": 100, "max_age": 40}

    def detect(self, ctx):
        i, c, trend = ctx.n - 1, candle(ctx), _trend(ctx)
        rng = st.dealing_range(ctx.df, i, self.p["strength"], self.p["lookback"])
        if not rng:
            return None
        for g in reversed(ctx.fvgs()):
            if g.idx >= i or i - g.idx > self.p["max_age"] or g.filled_idx not in (None, i):
                continue
            if trend > 0 and g.direction == "BUY" and c["l"] <= g.top and c["c"] > g.top and rng[0] > c["c"]:
                return self.make(ctx, "BUY", g.bottom, target=rng[0], reasons=["IRL tapped -> draw to ERL high"])
            if trend < 0 and g.direction == "SELL" and c["h"] >= g.bottom and c["c"] < g.bottom and rng[1] < c["c"]:
                return self.make(ctx, "SELL", g.top, target=rng[1], reasons=["IRL tapped -> draw to ERL low"])
        return None


class SmartMoneyReversal(Strategy):
    """Sweep -> CHOCH -> FVG sequence within a short window."""
    code, name, category, component = "smc_reversal", "Smart Money Reversal", CAT, C_SMC_ICT
    params = {"strength": 2, "window": 15, "rr": 3.0}
    regimes = ["REVERSAL", "SIDEWAYS", "WEAK_BULL", "WEAK_BEAR"]

    def detect(self, ctx):
        i, w = ctx.n - 1, self.p["window"]
        ev, _ = ctx.events(self.p["strength"])
        recent = [e for e in ev if e.kind == "CHOCH" and i - e.idx <= w // 2]
        if not recent:
            return None
        e = recent[-1]
        gaps = [g for g in ctx.fvgs() if e.idx - 3 <= g.idx <= i and g.direction == e.direction]
        if not gaps:
            return None
        win = ctx.df.iloc[-w:]
        prior = ctx.df.iloc[-4 * w:-w]
        c = candle(ctx)
        g = gaps[-1]
        if e.direction == "BUY" and win.low.min() < prior.low.min() and c["l"] <= g.top and c["c"] > g.bottom:
            return self.make(ctx, "BUY", float(win.low.min()), strength=0.75, reasons=["Sweep+CHOCH+FVG bullish"])
        if e.direction == "SELL" and win.high.max() > prior.high.max() and c["h"] >= g.bottom and c["c"] < g.top:
            return self.make(ctx, "SELL", float(win.high.max()), strength=0.75, reasons=["Sweep+CHOCH+FVG bearish"])
        return None


STRATEGIES = [OrderBlock, BreakerBlock, MitigationBlock, RejectionBlock, FairValueGap, InverseFVG, BalancedPriceRange,
              LiquiditySweep, BuySideLiquidity, SellSideLiquidity, Inducement, ChangeOfCharacter, BreakOfStructure,
              MarketStructureShift, Displacement, PremiumDiscount, Equilibrium, InternalRangeLiquidity,
              ExternalRangeLiquidity, SmartMoneyReversal]
_ = np
