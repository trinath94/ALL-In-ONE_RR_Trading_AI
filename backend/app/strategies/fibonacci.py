"""Fibonacci strategies (10). Legs are anchored on the latest break of structure so the
retracement is measured on a confirmed impulse, not an arbitrary swing."""
from __future__ import annotations

from app.strategies.base import C_INDICATOR, C_SMC_ICT, C_TREND, Strategy, StrategyContext, candle

CAT = "fibonacci"


def impulse_leg(ctx: StrategyContext, strength: int = 3, max_age: int = 60):
    """Return (direction, leg_low, leg_high) of the latest BOS/CHOCH impulse, measured up to the
    previous bar (the current bar is the retracement candidate)."""
    def build():
        ev, _ = ctx.events(strength)
        i = ctx.n - 1
        cands = [e for e in ev if i - e.idx <= max_age and e.idx < i]
        if not cands:
            return None
        e = cands[-1]
        lo_i = max(e.swing_idx - 20, 0)
        if e.direction == "BUY":
            seg_lo = ctx.df.low.iloc[lo_i:e.idx + 1]
            low = float(seg_lo.min())
            start = lo_i + int(seg_lo.values.argmin())
            high = float(ctx.df.high.iloc[start:i].max())
            return "BUY", low, high
        seg_hi = ctx.df.high.iloc[lo_i:e.idx + 1]
        high = float(seg_hi.max())
        start = lo_i + int(seg_hi.values.argmax())
        low = float(ctx.df.low.iloc[start:i].min())
        return "SELL", low, high
    return ctx.memo(("leg", strength, max_age), build)


def level_price(direction: str, low: float, high: float, ratio: float) -> float:
    return high - ratio * (high - low) if direction == "BUY" else low + ratio * (high - low)


class _FibLevel(Strategy):
    category, component = CAT, C_INDICATOR
    ratio = 0.618
    params = {"tol": 0.03}

    def detect(self, ctx):
        leg = impulse_leg(ctx)
        if not leg:
            return None
        d, lo, hi = leg
        rng = hi - lo
        lvl = level_price(d, lo, hi, self.ratio)
        tol = self.p["tol"] * rng
        c = candle(ctx)
        ext = hi + 0.272 * rng if d == "BUY" else lo - 0.272 * rng
        if d == "BUY" and abs(c["l"] - lvl) <= tol and c["c"] > lvl and c["bull"]:
            return self.make(ctx, "BUY", lo, target=ext, reasons=[f"Fib {self.ratio:.3f} hold at {lvl:.5f}"])
        if d == "SELL" and abs(c["h"] - lvl) <= tol and c["c"] < lvl and c["bear"]:
            return self.make(ctx, "SELL", hi, target=ext, reasons=[f"Fib {self.ratio:.3f} reject at {lvl:.5f}"])
        return None


class Fib382(_FibLevel):
    code, name, ratio = "fib_382", "Fibonacci 38.2% Retracement", 0.382


class Fib50(_FibLevel):
    code, name, ratio = "fib_50", "Fibonacci 50% Retracement", 0.5


class Fib618(_FibLevel):
    code, name, ratio = "fib_618", "Fibonacci 61.8% Retracement", 0.618


class Fib705(_FibLevel):
    code, name, ratio, component = "fib_705_ote", "Fibonacci 70.5% OTE", 0.705, C_SMC_ICT


class Fib786(_FibLevel):
    code, name, ratio = "fib_786", "Fibonacci 78.6% Retracement", 0.786


class FibExtensionTargets(Strategy):
    """Entry inside the 50-61.8% pocket; TP1 1.272, TP2 1.618 extension of the impulse."""
    code, name, category, component = "fib_extension_targets", "Fibonacci Extension Targets", CAT, C_INDICATOR

    def detect(self, ctx):
        leg = impulse_leg(ctx)
        if not leg:
            return None
        d, lo, hi = leg
        rng, c = hi - lo, candle(ctx)
        a, b = level_price(d, lo, hi, 0.5), level_price(d, lo, hi, 0.618)
        if d == "BUY" and b <= c["l"] <= a and c["bull"]:
            return self.make(ctx, "BUY", lo, target=lo + 1.272 * rng, tp2=lo + 1.618 * rng,
                             reasons=["Golden pocket entry, 1.272/1.618 targets"])
        if d == "SELL" and a <= c["h"] <= b and c["bear"]:
            return self.make(ctx, "SELL", hi, target=hi - 1.272 * rng, tp2=hi - 1.618 * rng,
                             reasons=["Golden pocket entry, 1.272/1.618 targets"])
        return None


class FibConfluence(Strategy):
    """Fib levels from two different leg scales (strength 3 and 6) cluster at the reaction price."""
    code, name, category, component = "fib_confluence", "Fibonacci Confluence", CAT, C_INDICATOR
    params = {"tol": 0.1}
    RATIOS = (0.382, 0.5, 0.618, 0.786)

    def detect(self, ctx):
        l1, l2 = impulse_leg(ctx, 3), impulse_leg(ctx, 6, 150)
        if not l1 or not l2 or l1[0] != l2[0]:
            return None
        d = l1[0]
        tol = self.p["tol"] * ctx.atr_last()
        c = candle(ctx)
        probe = c["l"] if d == "BUY" else c["h"]
        hits = [(r1, r2) for r1 in self.RATIOS for r2 in self.RATIOS
                if abs(level_price(d, l1[1], l1[2], r1) - probe) <= 3 * tol
                and abs(level_price(d, l2[1], l2[2], r2) - probe) <= 3 * tol]
        if not hits:
            return None
        if d == "BUY" and c["bull"]:
            return self.make(ctx, "BUY", min(l1[1], l2[1]), strength=0.7, reasons=[f"Fib confluence {hits[0]}"])
        if d == "SELL" and c["bear"]:
            return self.make(ctx, "SELL", max(l1[2], l2[2]), strength=0.7, reasons=[f"Fib confluence {hits[0]}"])
        return None


class FibFvg(Strategy):
    """Retracement into the 50-79% zone that overlaps an unfilled FVG of the impulse."""
    code, name, category, component = "fib_fvg", "Fibonacci + FVG", CAT, C_SMC_ICT

    def detect(self, ctx):
        leg = impulse_leg(ctx)
        if not leg:
            return None
        d, lo, hi = leg
        z1, z2 = sorted([level_price(d, lo, hi, 0.5), level_price(d, lo, hi, 0.79)])
        c, i = candle(ctx), ctx.n - 1
        for g in reversed(ctx.fvgs(0.15)):
            if g.direction != d or g.idx >= i or (g.filled_idx is not None and g.filled_idx < i):
                continue
            top, bot = min(g.top, z2), max(g.bottom, z1)
            if top <= bot:
                continue
            if d == "BUY" and c["l"] <= top and c["c"] > bot and c["bull"]:
                return self.make(ctx, "BUY", lo, target=hi, strength=0.7, reasons=["FVG inside fib 50-79% zone"])
            if d == "SELL" and c["h"] >= bot and c["c"] < top and c["bear"]:
                return self.make(ctx, "SELL", hi, target=lo, strength=0.7, reasons=["FVG inside fib 50-79% zone"])
        return None


class FibOrderBlock(Strategy):
    """Retracement into the 50-79% zone that overlaps the impulse's order block."""
    code, name, category, component = "fib_ob", "Fibonacci + Order Block", CAT, C_SMC_ICT

    def detect(self, ctx):
        leg = impulse_leg(ctx)
        if not leg:
            return None
        d, lo, hi = leg
        z1, z2 = sorted([level_price(d, lo, hi, 0.5), level_price(d, lo, hi, 0.79)])
        c = candle(ctx)
        for z in reversed(ctx.order_blocks()):
            if z.direction != d or z.broken_idx is not None:
                continue
            top, bot = min(z.top, z2), max(z.bottom, z1)
            if top <= bot:
                continue
            if d == "BUY" and c["l"] <= top and c["c"] > bot and c["bull"]:
                return self.make(ctx, "BUY", min(lo, z.bottom), target=hi, strength=0.7, reasons=["OB inside fib zone"])
            if d == "SELL" and c["h"] >= bot and c["c"] < top and c["bear"]:
                return self.make(ctx, "SELL", max(hi, z.top), target=lo, strength=0.7, reasons=["OB inside fib zone"])
        return None


class FibTrendContinuation(Strategy):
    """Strong trend (ADX > 25): shallow 23.6-38.2% pullback then continuation close."""
    code, name, category, component = "fib_trend_continuation", "Fibonacci Trend Continuation", CAT, C_TREND
    regimes = ["STRONG_BULL", "STRONG_BEAR"]

    def detect(self, ctx):
        if ctx.adx().adx.iloc[-1] < 25:
            return None
        leg = impulse_leg(ctx)
        if not leg:
            return None
        d, lo, hi = leg
        a, b = sorted([level_price(d, lo, hi, 0.236), level_price(d, lo, hi, 0.382)])
        c = candle(ctx)
        if d == "BUY" and a <= c["l"] <= b and c["bull"] and c["c"] > candle(ctx, 2)["h"]:
            return self.make(ctx, "BUY", level_price(d, lo, hi, 0.618), target=hi + 0.618 * (hi - lo),
                             reasons=["Shallow fib pullback in strong uptrend"])
        if d == "SELL" and a <= c["h"] <= b and c["bear"] and c["c"] < candle(ctx, 2)["l"]:
            return self.make(ctx, "SELL", level_price(d, lo, hi, 0.618), target=lo - 0.618 * (hi - lo),
                             reasons=["Shallow fib pullback in strong downtrend"])
        return None


STRATEGIES = [Fib382, Fib50, Fib618, Fib705, Fib786, FibExtensionTargets, FibConfluence, FibFvg, FibOrderBlock,
              FibTrendContinuation]
