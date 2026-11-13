"""Advanced reversal strategies (10)."""
from __future__ import annotations

import numpy as np

from app.analysis import indicators as ind
from app.strategies.base import C_INDICATOR, C_PRICE_ACTION, C_VOLUME, Strategy, StrategyContext, candle
from app.strategies.indicators import _divergence
from app.strategies.price_action import levels

CAT = "reversal"
REV = ["REVERSAL", "SIDEWAYS", "HIGH_VOLATILITY"]


class DivergenceReversal(Strategy):
    """Divergence on RSI *and* MACD at the same swing, confirmed by a close beyond the prior bar."""
    code, name, category, component = "rev_divergence", "Divergence Reversal", CAT, C_INDICATOR
    regimes = REV

    def detect(self, ctx):
        r, m = _divergence(ctx, ctx.rsi()), _divergence(ctx, ctx.macd().macd)
        if r and m and r[0] == m[0]:
            return self.make(ctx, r[0], r[1], strength=0.7, reasons=["RSI + MACD divergence"])
        return None


class ExhaustionReversal(Strategy):
    """7+ consecutive closes in one direction, then a close beyond the prior bar the other way."""
    code, name, category, component = "rev_exhaustion", "Exhaustion Reversal", CAT, C_PRICE_ACTION
    params = {"run": 7}
    regimes = REV

    def detect(self, ctx):
        d = np.sign(ctx.close.diff()).iloc[-self.p["run"] - 1:-1]
        c, p = candle(ctx), candle(ctx, 2)
        if (d > 0).all() and c["c"] < p["l"]:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-3:].max()), reasons=["Up-run exhaustion"])
        if (d < 0).all() and c["c"] > p["h"]:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-3:].min()), reasons=["Down-run exhaustion"])
        return None


class FailedBreakout(Strategy):
    """Close above the 20-bar high within the last 3 bars, now closed back below it."""
    code, name, category, component = "rev_failed_breakout", "Failed Breakout", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        lvl = float(ctx.df.high.iloc[-24:-4].max())
        recent = ctx.df.close.iloc[-4:-1]
        if (recent > lvl).any() and ctx.last() < lvl:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-4:].max()), reasons=[f"Failed breakout above {lvl:.5f}"])
        return None


class FailedBreakdown(Strategy):
    """Close below the 20-bar low within the last 3 bars, now closed back above it."""
    code, name, category, component = "rev_failed_breakdown", "Failed Breakdown", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        lvl = float(ctx.df.low.iloc[-24:-4].min())
        recent = ctx.df.close.iloc[-4:-1]
        if (recent < lvl).any() and ctx.last() > lvl:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-4:].min()), reasons=[f"Failed breakdown below {lvl:.5f}"])
        return None


class ClimaxReversal(Strategy):
    """Range > 2.5 ATR bar at a 50-bar extreme followed by an inside/opposite close."""
    code, name, category, component = "rev_climax", "Climax Reversal", CAT, C_VOLUME
    regimes = REV

    def detect(self, ctx):
        p, c = candle(ctx, 2), candle(ctx)
        a = float(ctx.atr().iloc[-3])
        if p["range"] < 2.5 * a:
            return None
        if p["bull"] and p["h"] >= ctx.df.high.iloc[-52:-1].max() and c["c"] < p["c"] - 0.3 * p["body"]:
            return self.make(ctx, "SELL", p["h"], reasons=["Buying climax rejected"])
        if p["bear"] and p["l"] <= ctx.df.low.iloc[-52:-1].min() and c["c"] > p["c"] + 0.3 * p["body"]:
            return self.make(ctx, "BUY", p["l"], reasons=["Selling climax rejected"])
        return None


class ParabolicReversal(Strategy):
    """Accelerating slope (2nd derivative of EMA10 > 0) with RSI > 80, then lower close."""
    code, name, category, component = "rev_parabolic", "Parabolic Reversal", CAT, C_INDICATOR
    regimes = REV

    def detect(self, ctx):
        e = ctx.ema(10)
        acc = e.diff().diff().iloc[-6:-1]
        r = ctx.rsi().iloc[-2]
        c, p = candle(ctx), candle(ctx, 2)
        if (acc > 0).sum() >= 4 and r > 80 and c["c"] < p["l"]:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-3:].max()), target=float(ctx.ema(20).iloc[-1]),
                             reasons=["Parabolic blow-off reversal"])
        if (acc < 0).sum() >= 4 and r < 20 and c["c"] > p["h"]:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-3:].min()), target=float(ctx.ema(20).iloc[-1]),
                             reasons=["Capitulation reversal"])
        return None


class MeanReversion(Strategy):
    """Close z-score vs 50-bar mean beyond +/-2.5, turning back; target the mean."""
    code, name, category, component = "rev_mean_reversion", "Mean Reversion", CAT, C_INDICATOR
    params = {"n": 50, "z": 2.5}
    regimes = ["SIDEWAYS", "CONSOLIDATION"]

    def detect(self, ctx):
        z = ind.zscore(ctx.close, self.p["n"])
        m = float(ctx.sma(self.p["n"]).iloc[-1])
        if z.iloc[-2] < -self.p["z"] and z.iloc[-1] > z.iloc[-2]:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-3:].min()), target=m, reasons=[f"z={z.iloc[-2]:.1f} reverting"])
        if z.iloc[-2] > self.p["z"] and z.iloc[-1] < z.iloc[-2]:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-3:].max()), target=m, reasons=[f"z={z.iloc[-2]:.1f} reverting"])
        return None


class BollingerReversal(Strategy):
    """Close outside the band followed by a close back inside; target the middle band."""
    code, name, category, component = "rev_bollinger", "Bollinger Band Reversal", CAT, C_INDICATOR
    regimes = ["SIDEWAYS", "CONSOLIDATION", "REVERSAL"]

    def detect(self, ctx):
        b = ctx.bb()
        c = ctx.close
        if c.iloc[-2] < b.lower.iloc[-2] and c.iloc[-1] > b.lower.iloc[-1]:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-2:].min()), target=float(b.mid.iloc[-1]),
                             reasons=["Re-entry inside lower band"])
        if c.iloc[-2] > b.upper.iloc[-2] and c.iloc[-1] < b.upper.iloc[-1]:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-2:].max()), target=float(b.mid.iloc[-1]),
                             reasons=["Re-entry inside upper band"])
        return None


class VwapReversion(Strategy):
    """Price > 2 standard deviations from session VWAP, reverting toward it."""
    code, name, category, component = "rev_vwap", "VWAP Reversion", CAT, C_VOLUME
    requires = ["tick_volume"]
    timeframes = ["M5", "M15"]

    def detect(self, ctx: StrategyContext):
        v = ind.vwap_session(ctx.df)
        dev = (ctx.close - v)
        sd = dev.rolling(50).std()
        z = dev / sd
        if np.isnan(z.iloc[-2]):
            return None
        if z.iloc[-2] < -2 and z.iloc[-1] > z.iloc[-2] and candle(ctx)["bull"]:
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-3:].min()), target=float(v.iloc[-1]), reasons=["VWAP -2σ reversion"])
        if z.iloc[-2] > 2 and z.iloc[-1] < z.iloc[-2] and candle(ctx)["bear"]:
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-3:].max()), target=float(v.iloc[-1]), reasons=["VWAP +2σ reversion"])
        return None


class SRReversal(Strategy):
    """Reversal pattern (engulfing/pin) on a 3+ touch S/R level with RSI at an extreme."""
    code, name, category, component = "rev_sr", "Support and Resistance Reversal", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        from app.strategies.price_action import _engulf, _pin
        d = _engulf(ctx) or _pin(ctx)
        if not d:
            return None
        c, tol, r = candle(ctx), 0.3 * ctx.atr_last(), ctx.rsi().iloc[-1]
        for lvl, touches in levels(ctx, 3, 0.25, 3):
            if d == "BUY" and abs(c["l"] - lvl) <= tol and r < 45:
                return self.make(ctx, "BUY", c["l"], strength=0.65, reasons=[f"Reversal at {touches}-touch support"])
            if d == "SELL" and abs(c["h"] - lvl) <= tol and r > 55:
                return self.make(ctx, "SELL", c["h"], strength=0.65, reasons=[f"Reversal at {touches}-touch resistance"])
        return None


STRATEGIES = [DivergenceReversal, ExhaustionReversal, FailedBreakout, FailedBreakdown, ClimaxReversal,
              ParabolicReversal, MeanReversion, BollingerReversal, VwapReversion, SRReversal]
