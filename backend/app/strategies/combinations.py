"""Multi-strategy combinations (15). Each combination composes component detectors on the same
context (memoised), plus explicit confluence conditions — it is not a simple vote."""
from __future__ import annotations

import numpy as np

from app.analysis import indicators as ind
from app.analysis import sessions as ss
from app.strategies import fibonacci as fib
from app.strategies import ict, indicators as ind_s, liquidity, price_action as pa, smc, trend, volume
from app.strategies.base import (C_MTF, C_SMC_ICT, C_TREND, CAP_PEERS, CAP_TICK_VOLUME, Strategy, StrategyContext,
                                 cross_up, cross_down, candle)

CAT = "combinations"


def fires(ctx: StrategyContext, cls, **params):
    key = ("fires", cls.code, tuple(sorted(params.items())))
    return ctx.memo(key, lambda: cls(**params).evaluate(ctx))


def _boost(sig, code, reasons, strength=0.8):
    if sig is None:
        return None
    sig.strategy = code
    sig.strength = max(sig.strength, strength)
    sig.reasons = reasons + sig.reasons
    return sig


class _Combo(Strategy):
    category, component = CAT, C_SMC_ICT

    def evaluate(self, ctx):
        ok, _ = self.available(ctx)
        if not ok:
            return None
        s = self.detect(ctx)
        if s is None or not s.valid():
            return None
        s.strategy, s.component = self.code, self.component
        s.meta["components"] = s.meta.get("components", [])
        return s


class SmcIct(_Combo):
    """SMC entry (OB / FVG / BOS) inside an ICT kill zone and aligned with HTF bias."""
    code, name = "combo_smc_ict", "SMC + ICT"
    timeframes = ["M5", "M15"]

    def detect(self, ctx):
        if ctx.timeframe not in ict.INTRADAY:
            return None
        if not any(bool(ss.session_mask(ctx.df.index[-1:], k).iloc[-1]) for k in ("LONDON_KZ", "NY_AM_KZ", "NY_PM_KZ")):
            return None
        bias = ctx.htf_bias()
        for cls in (smc.OrderBlock, smc.FairValueGap, smc.BreakOfStructure):
            s = fires(ctx, cls)
            if s and bias == (1 if s.direction == "BUY" else -1):
                return _boost(s, self.code, [f"{cls.name} in kill zone with HTF bias"])
        return None


class BosFvg(_Combo):
    """FVG retrace where the gap belongs to a BOS leg in the last 15 bars."""
    code, name = "combo_bos_fvg", "BOS + FVG"

    def detect(self, ctx):
        s = fires(ctx, smc.FairValueGap)
        if not s:
            return None
        ev, _ = ctx.events()
        if any(e.kind == "BOS" and e.direction == s.direction and ctx.n - 1 - e.idx <= 15 for e in ev):
            return _boost(s, self.code, ["FVG created by recent BOS displacement"])
        return None


class SweepChoch(_Combo):
    """CHOCH now, preceded within 10 bars by a sweep of the opposite-side swing."""
    code, name, component = "combo_sweep_choch", "Liquidity Sweep + CHOCH", C_SMC_ICT

    def detect(self, ctx):
        s = fires(ctx, smc.ChangeOfCharacter)
        if not s:
            return None
        win, prior = ctx.df.iloc[-11:-1], ctx.df.iloc[-60:-11]
        if s.direction == "BUY" and win.low.min() < prior.low.min():
            return _boost(s, self.code, ["Sell-side sweep then bullish CHOCH"], 0.8)
        if s.direction == "SELL" and win.high.max() > prior.high.max():
            return _boost(s, self.code, ["Buy-side sweep then bearish CHOCH"], 0.8)
        return None


class ObFvg(_Combo):
    """Order-block retest where an FVG formed within 3 bars after the OB candle."""
    code, name = "combo_ob_fvg", "Order Block + FVG"

    def detect(self, ctx):
        s = fires(ctx, smc.OrderBlock)
        if not s:
            return None
        zones = [z for z in ctx.order_blocks() if z.mitigated_idx == ctx.n - 1 and z.direction == s.direction]
        for z in zones:
            k = z.meta["candle"]
            if any(g.direction == z.direction and k < g.idx <= k + 3 for g in ctx.fvgs(0.1)):
                return _boost(s, self.code, ["OB with adjacent FVG"])
        return None


class Ema200Rsi(_Combo):
    """Above EMA200, RSI dips below 40 and crosses back up (and mirror)."""
    code, name, component = "combo_ema200_rsi", "EMA 200 + RSI", C_TREND
    min_bars = 220

    def detect(self, ctx):
        e, r, c = ctx.ema(200).iloc[-1], ctx.rsi(), ctx.last()
        if c > e and cross_up(r, 40.0):
            return self.make(ctx, "BUY", None, reasons=["Above EMA200, RSI pullback over"])
        if c < e and cross_down(r, 60.0):
            return self.make(ctx, "SELL", None, reasons=["Below EMA200, RSI bounce over"])
        return None


class EmaMacd(_Combo):
    """EMA20/50 trend plus a MACD signal cross in the same direction."""
    code, name, component = "combo_ema_macd", "EMA + MACD", C_TREND

    def detect(self, ctx):
        f, s, m = ctx.ema(20).iloc[-1], ctx.ema(50).iloc[-1], ctx.macd()
        if f > s and cross_up(m.macd, m.signal):
            return self.make(ctx, "BUY", None, reasons=["EMA uptrend + MACD bull cross"])
        if f < s and cross_down(m.macd, m.signal):
            return self.make(ctx, "SELL", None, reasons=["EMA downtrend + MACD bear cross"])
        return None


class SupertrendAdx(_Combo):
    """Supertrend flip confirmed by ADX > 20 and rising."""
    code, name, component = "combo_supertrend_adx", "Supertrend + ADX", C_TREND

    def detect(self, ctx):
        s = fires(ctx, trend.Supertrend)
        a = ctx.adx().adx
        if s and a.iloc[-1] > 20 and a.iloc[-1] > a.iloc[-3]:
            return _boost(s, self.code, [f"ADX {a.iloc[-1]:.0f} rising"], 0.7)
        return None


class VwapVolumeProfile(_Combo):
    """VWAP reclaim/loss occurring at the value-area edge or POC (tick volume)."""
    code, name = "combo_vwap_vp", "VWAP + Volume Profile"
    requires = [CAP_TICK_VOLUME]
    timeframes = ["M5", "M15"]

    def detect(self, ctx):
        s = fires(ctx, ind_s.VwapStrategy)
        if not s:
            return None
        vp = ind.volume_profile(ctx.df.iloc[-101:-1])
        tol = 0.5 * ctx.atr_last()
        if vp["poc"] and any(abs(ctx.last() - v) <= tol for v in (vp["poc"], vp["val"], vp["vah"])):
            return _boost(s, self.code, ["VWAP signal at volume-profile level"], 0.7)
        return None


class RsiBollinger(_Combo):
    """Close back inside the lower (upper) band after RSI < 30 (> 70)."""
    code, name = "combo_rsi_bb", "RSI + Bollinger Bands"

    def detect(self, ctx):
        s = fires(ctx, __import__("app.strategies.reversal", fromlist=["BollingerReversal"]).BollingerReversal)
        r = ctx.rsi().iloc[-2]
        if s and ((s.direction == "BUY" and r < 30) or (s.direction == "SELL" and r > 70)):
            return _boost(s, self.code, [f"RSI {r:.0f} at band extreme"], 0.7)
        return None


class FibStructure(_Combo):
    """61.8%/70.5% retracement of a BOS leg aligned with HTF bias."""
    code, name, component = "combo_fib_structure", "Fibonacci + Market Structure", C_MTF

    def detect(self, ctx):
        bias = ctx.htf_bias()
        for cls in (fib.Fib618, fib.Fib705):
            s = fires(ctx, cls)
            if s and (bias == 0 or bias == (1 if s.direction == "BUY" else -1)):
                return _boost(s, self.code, ["Fib retracement of structural leg, HTF aligned"])
        return None


class WyckoffVolumeProfile(_Combo):
    """Wyckoff spring/upthrust located at the value-area low/high."""
    code, name = "combo_wyckoff_vp", "Wyckoff + Volume Profile"
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        vp = ind.volume_profile(ctx.df.iloc[-101:-1])
        if vp["poc"] is None:
            return None
        tol = 0.5 * ctx.atr_last()
        s = fires(ctx, volume.WyckoffSpring)
        if s and abs(candle(ctx)["l"] - vp["val"]) <= 2 * tol:
            return _boost(s, self.code, ["Spring at VAL"])
        s = fires(ctx, volume.WyckoffUpthrust)
        if s and abs(candle(ctx)["h"] - vp["vah"]) <= 2 * tol:
            return _boost(s, self.code, ["Upthrust at VAH"])
        return None


class IctSmt(_Combo):
    """Turtle soup / liquidity raid confirmed by SMT divergence with a correlated peer."""
    code, name = "combo_ict_smt", "ICT + SMT Divergence"
    requires = [CAP_PEERS]

    def detect(self, ctx):
        smt = fires(ctx, ict.SmtDivergence)
        if not smt:
            return None
        for cls in (ict.TurtleSoup, smc.LiquiditySweep):
            s = fires(ctx, cls)
            if s and s.direction == smt.direction:
                return _boost(s, self.code, ["Sweep confirmed by SMT divergence"], 0.85)
        return None


class SweepOrderBlock(_Combo):
    """OB retest where the impulse that created the OB began with a liquidity sweep."""
    code, name = "combo_sweep_ob", "Liquidity Sweep + Order Block"

    def detect(self, ctx):
        s = fires(ctx, smc.OrderBlock)
        if not s:
            return None
        for z in ctx.order_blocks():
            if z.mitigated_idx != ctx.n - 1 or z.direction != s.direction:
                continue
            k = z.meta["candle"]
            prior = ctx.df.iloc[max(k - 40, 0):max(k - 2, 1)]
            if len(prior) == 0:
                continue
            cnd = ctx.df.iloc[max(k - 2, 0):k + 2]
            if (s.direction == "BUY" and cnd.low.min() < prior.low.min()) or \
                    (s.direction == "SELL" and cnd.high.max() > prior.high.max()):
                return _boost(s, self.code, ["OB origin swept liquidity"], 0.85)
        return None


class BreakoutAtr(_Combo):
    """Donchian-20 breakout while ATR(14) > 1.2x its 50-bar average."""
    code, name, component = "combo_breakout_atr", "Breakout + ATR", C_TREND

    def detect(self, ctx):
        s = fires(ctx, ind_s.DonchianBreakout)
        a = ctx.atr()
        if s and a.iloc[-1] > 1.2 * a.iloc[-50:].mean():
            return _boost(s, self.code, ["ATR expansion confirms breakout"], 0.7)
        return None


class TrendPullbackMomentum(_Combo):
    """EMA50 > EMA200 trend, pullback to EMA20, then a momentum close beyond the prior high."""
    code, name, component = "combo_trend_pullback_momentum", "Trend + Pullback + Momentum", C_TREND
    min_bars = 220

    def detect(self, ctx):
        e20, e50, e200 = ctx.ema(20), ctx.ema(50).iloc[-1], ctx.ema(200).iloc[-1]
        c, p = candle(ctx), candle(ctx, 2)
        touched = (ctx.df.low.iloc[-4:-1] <= e20.iloc[-4:-1]).any()
        touched_s = (ctx.df.high.iloc[-4:-1] >= e20.iloc[-4:-1]).any()
        body_avg = (ctx.df.close - ctx.df.open).abs().iloc[-21:-1].mean()
        if e50 > e200 and touched and c["bull"] and c["c"] > p["h"] and c["body"] > 1.3 * body_avg:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-4:].min()), reasons=["Trend+pullback+momentum long"])
        if e50 < e200 and touched_s and c["bear"] and c["c"] < p["l"] and c["body"] > 1.3 * body_avg:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-4:].max()), reasons=["Trend+pullback+momentum short"])
        return None


STRATEGIES = [SmcIct, BosFvg, SweepChoch, ObFvg, Ema200Rsi, EmaMacd, SupertrendAdx, VwapVolumeProfile, RsiBollinger,
              FibStructure, WyckoffVolumeProfile, IctSmt, SweepOrderBlock, BreakoutAtr, TrendPullbackMomentum]
_ = (liquidity, pa, np)
