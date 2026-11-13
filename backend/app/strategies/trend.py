"""Trend-following strategies (15)."""
from __future__ import annotations

import numpy as np

from app.analysis import indicators as ind
from app.strategies.base import C_STRUCTURE, C_TREND, Strategy, candle, cross_down, cross_up, last_swing

CAT = "trend"
TRENDING = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "BREAKOUT"]


class _EmaCross(Strategy):
    """EMA crossover entry with ATR stop."""
    category, component = CAT, C_TREND
    regimes = TRENDING
    params = {"fast": 9, "slow": 21, "sl_atr": 1.5, "rr": 2.0}
    kind = "ema"

    def detect(self, ctx):
        f = ctx.ema(self.p["fast"]) if self.kind == "ema" else ctx.sma(self.p["fast"])
        s = ctx.ema(self.p["slow"]) if self.kind == "ema" else ctx.sma(self.p["slow"])
        if np.isnan(s.iloc[-2]):
            return None
        if cross_up(f, s):
            return self.make(ctx, "BUY", None, reasons=[f"{self.kind.upper()} {self.p['fast']}/{self.p['slow']} bull cross"])
        if cross_down(f, s):
            return self.make(ctx, "SELL", None, reasons=[f"{self.kind.upper()} {self.p['fast']}/{self.p['slow']} bear cross"])
        return None


class Ema9_21(_EmaCross):
    code, name = "trend_ema_9_21", "EMA 9/21 Crossover"
    params = {**_EmaCross.params, "fast": 9, "slow": 21}


class Ema20_50(_EmaCross):
    code, name = "trend_ema_20_50", "EMA 20/50 Crossover"
    params = {**_EmaCross.params, "fast": 20, "slow": 50}


class Ema50_200(_EmaCross):
    code, name = "trend_ema_50_200", "EMA 50/200 Crossover"
    params = {**_EmaCross.params, "fast": 50, "slow": 200, "sl_atr": 2.5}
    min_bars = 220
    timeframes = ["H1", "H4", "D1"]


class Sma(_EmaCross):
    code, name, kind = "trend_sma_cross", "SMA Crossover", "sma"
    params = {**_EmaCross.params, "fast": 20, "slow": 50}


class Ema200Trend(Strategy):
    """Price above a rising EMA200: buy pullbacks that close back above EMA50 (and inverse)."""
    code, name, category, component = "trend_ema200", "EMA 200 Trend Strategy", CAT, C_TREND
    regimes = TRENDING
    min_bars = 220

    def detect(self, ctx):
        e200, e50 = ctx.ema(200), ctx.ema(50)
        c, slope = candle(ctx), e200.iloc[-1] - e200.iloc[-10]
        if c["c"] > e200.iloc[-1] and slope > 0 and cross_up(ctx.close, e50):
            return self.make(ctx, "BUY", None, reasons=["Above rising EMA200, reclaim EMA50"])
        if c["c"] < e200.iloc[-1] and slope < 0 and cross_down(ctx.close, e50):
            return self.make(ctx, "SELL", None, reasons=["Below falling EMA200, lose EMA50"])
        return None


class MaRibbon(Strategy):
    """Ribbon of 8 EMAs (8..55) becomes fully ordered (fan) on this bar."""
    code, name, category, component = "trend_ma_ribbon", "Moving Average Ribbon", CAT, C_TREND
    params = {"periods": [8, 13, 21, 34, 55], "sl_atr": 2.0}
    regimes = TRENDING

    def detect(self, ctx):
        e = np.array([[ctx.ema(p).iloc[-2], ctx.ema(p).iloc[-1]] for p in self.p["periods"]])
        if np.isnan(e).any():
            return None
        bull = lambda col: bool(np.all(np.diff(e[:, col]) < 0))  # noqa: E731 shorter EMA highest
        bear = lambda col: bool(np.all(np.diff(e[:, col]) > 0))  # noqa: E731
        if bull(1) and not bull(0):
            return self.make(ctx, "BUY", None, reasons=["EMA ribbon fanned bullish"])
        if bear(1) and not bear(0):
            return self.make(ctx, "SELL", None, reasons=["EMA ribbon fanned bearish"])
        return None


class Supertrend(Strategy):
    """Supertrend direction flip; stop at the supertrend line."""
    code, name, category, component = "trend_supertrend", "Supertrend Strategy", CAT, C_TREND
    params = {"n": 10, "mult": 3.0, "rr": 2.0}

    def detect(self, ctx):
        s = ctx.memo(("st", self.p["n"], self.p["mult"]), lambda: ind.supertrend(ctx.df, self.p["n"], self.p["mult"]))
        t0, t1, line = s.trend.iloc[-2], s.trend.iloc[-1], s.line.iloc[-1]
        if t0 < 0 < t1:
            return self.make(ctx, "BUY", float(line), reasons=["Supertrend flipped bullish"])
        if t0 > 0 > t1:
            return self.make(ctx, "SELL", float(line), reasons=["Supertrend flipped bearish"])
        return None


class MacdTrend(Strategy):
    """MACD line crosses zero with histogram expanding, price on the same side of EMA100."""
    code, name, category, component = "trend_macd", "MACD Trend Strategy", CAT, C_TREND
    regimes = TRENDING
    min_bars = 120

    def detect(self, ctx):
        m, e = ctx.macd(), ctx.ema(100).iloc[-1]
        c = ctx.last()
        if cross_up(m.macd, 0.0) and m["hist"].iloc[-1] > m["hist"].iloc[-2] and c > e:
            return self.make(ctx, "BUY", None, reasons=["MACD above zero, trend up"])
        if cross_down(m.macd, 0.0) and m["hist"].iloc[-1] < m["hist"].iloc[-2] and c < e:
            return self.make(ctx, "SELL", None, reasons=["MACD below zero, trend down"])
        return None


class AdxTrend(Strategy):
    """ADX crosses above threshold with DI alignment."""
    code, name, category, component = "trend_adx", "ADX Trend Strength", CAT, C_TREND
    params = {"threshold": 25}

    def detect(self, ctx):
        a = ctx.adx()
        if not cross_up(a.adx, float(self.p["threshold"])):
            return None
        if a.pdi.iloc[-1] > a.mdi.iloc[-1]:
            return self.make(ctx, "BUY", None, reasons=[f"ADX>{self.p['threshold']} with +DI lead"])
        return self.make(ctx, "SELL", None, reasons=[f"ADX>{self.p['threshold']} with -DI lead"])


class ParabolicSar(Strategy):
    """PSAR flip, filtered by EMA50 direction; stop at the new SAR."""
    code, name, category, component = "trend_psar", "Parabolic SAR", CAT, C_TREND
    regimes = TRENDING

    def detect(self, ctx):
        p = ctx.memo(("psar",), lambda: ind.psar(ctx.df))
        e = ctx.ema(50)
        up = e.iloc[-1] > e.iloc[-5]
        if p.trend.iloc[-2] < 0 < p.trend.iloc[-1] and up:
            return self.make(ctx, "BUY", float(p.sar.iloc[-1]), reasons=["PSAR flip up"])
        if p.trend.iloc[-2] > 0 > p.trend.iloc[-1] and not up:
            return self.make(ctx, "SELL", float(p.sar.iloc[-1]), reasons=["PSAR flip down"])
        return None


class Ichimoku(Strategy):
    """Tenkan/Kijun cross above (below) the cloud with price outside the cloud."""
    code, name, category, component = "trend_ichimoku", "Ichimoku Cloud", CAT, C_TREND
    min_bars = 120

    def detect(self, ctx):
        i = ctx.memo(("ichi",), lambda: ind.ichimoku(ctx.df)).iloc[-2:]
        top, bot = max(i.span_a.iloc[-1], i.span_b.iloc[-1]), min(i.span_a.iloc[-1], i.span_b.iloc[-1])
        if np.isnan(top):
            return None
        c = ctx.last()
        tk_up = i.tenkan.iloc[-2] <= i.kijun.iloc[-2] and i.tenkan.iloc[-1] > i.kijun.iloc[-1]
        tk_dn = i.tenkan.iloc[-2] >= i.kijun.iloc[-2] and i.tenkan.iloc[-1] < i.kijun.iloc[-1]
        if tk_up and c > top:
            return self.make(ctx, "BUY", float(min(i.kijun.iloc[-1], top)), reasons=["TK cross above cloud"])
        if tk_dn and c < bot:
            return self.make(ctx, "SELL", float(max(i.kijun.iloc[-1], bot)), reasons=["TK cross below cloud"])
        return None


class DonchianTrend(Strategy):
    """Turtle-style: 55-bar breakout entry, 20-bar opposite channel as stop."""
    code, name, category, component = "trend_donchian", "Donchian Channel Trend", CAT, C_TREND
    params = {"entry": 55, "exit": 20, "rr": 3.0}
    regimes = TRENDING

    def detect(self, ctx):
        d = ind.donchian(ctx.df, self.p["entry"]).iloc[-1]
        x = ind.donchian(ctx.df, self.p["exit"]).iloc[-1]
        c = ctx.last()
        if np.isnan(d.upper):
            return None
        if c > d.upper:
            return self.make(ctx, "BUY", float(x.lower), reasons=[f"{self.p['entry']}-bar high breakout"])
        if c < d.lower:
            return self.make(ctx, "SELL", float(x.upper), reasons=[f"{self.p['entry']}-bar low breakout"])
        return None


class TrendlineBreakout(Strategy):
    """Close through a falling (rising) trendline connecting the last two swing highs (lows)."""
    code, name, category, component = "trend_trendline_breakout", "Trendline Breakout", CAT, C_TREND

    def detect(self, ctx):
        i, prev, c = ctx.n - 1, ctx.df.close.iloc[-2], ctx.last()
        hs = [s for s in ctx.known_swings() if s.kind == "H"][-2:]
        if len(hs) == 2 and hs[1].price < hs[0].price:
            y0 = hs[0].price + (hs[1].price - hs[0].price) * (i - 1 - hs[0].idx) / (hs[1].idx - hs[0].idx)
            y1 = hs[0].price + (hs[1].price - hs[0].price) * (i - hs[0].idx) / (hs[1].idx - hs[0].idx)
            if prev <= y0 and c > y1:
                sl = last_swing(ctx, "L")
                return self.make(ctx, "BUY", sl.price if sl else None, reasons=["Falling trendline broken"])
        ls = [s for s in ctx.known_swings() if s.kind == "L"][-2:]
        if len(ls) == 2 and ls[1].price > ls[0].price:
            y0 = ls[0].price + (ls[1].price - ls[0].price) * (i - 1 - ls[0].idx) / (ls[1].idx - ls[0].idx)
            y1 = ls[0].price + (ls[1].price - ls[0].price) * (i - ls[0].idx) / (ls[1].idx - ls[0].idx)
            if prev >= y0 and c < y1:
                sh = last_swing(ctx, "H")
                return self.make(ctx, "SELL", sh.price if sh else None, reasons=["Rising trendline broken"])
        return None


class HigherHighsHigherLows(Strategy):
    """Two higher highs and two higher lows; buy the confirmation of the latest higher low."""
    code, name, category, component = "trend_hh_hl", "Higher Highs and Higher Lows", CAT, C_STRUCTURE

    def detect(self, ctx):
        sw = ctx.known_swings()
        hs, ls = [s for s in sw if s.kind == "H"][-2:], [s for s in sw if s.kind == "L"][-2:]
        if len(hs) < 2 or len(ls) < 2:
            return None
        if hs[1].price > hs[0].price and ls[1].price > ls[0].price and ls[1].confirmed == ctx.n - 1:
            return self.make(ctx, "BUY", ls[1].price, target=None, reasons=["HH/HL sequence, new HL confirmed"])
        return None


class LowerHighsLowerLows(Strategy):
    """Two lower highs and two lower lows; sell the confirmation of the latest lower high."""
    code, name, category, component = "trend_lh_ll", "Lower Highs and Lower Lows", CAT, C_STRUCTURE

    def detect(self, ctx):
        sw = ctx.known_swings()
        hs, ls = [s for s in sw if s.kind == "H"][-2:], [s for s in sw if s.kind == "L"][-2:]
        if len(hs) < 2 or len(ls) < 2:
            return None
        if hs[1].price < hs[0].price and ls[1].price < ls[0].price and hs[1].confirmed == ctx.n - 1:
            return self.make(ctx, "SELL", hs[1].price, reasons=["LH/LL sequence, new LH confirmed"])
        return None


STRATEGIES = [Ema9_21, Ema20_50, Ema50_200, Ema200Trend, Sma, MaRibbon, Supertrend, MacdTrend, AdxTrend, ParabolicSar,
              Ichimoku, DonchianTrend, TrendlineBreakout, HigherHighsHigherLows, LowerHighsLowerLows]
