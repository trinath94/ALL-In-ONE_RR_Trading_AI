"""Indicator-based strategies (25).

Volume-dependent indicators (MFI, OBV, VWAP, volume profile) use broker *tick volume*
on OTC instruments; signals are tagged accordingly so they are never presented as
exchange volume.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.analysis import sessions as ss
from app.analysis import structure as st
from app.strategies.base import (C_INDICATOR, C_VOLUME, CAP_TICK_VOLUME, Strategy, StrategyContext, candle,
                                 cross_down, cross_up, last_swing)

CAT = "indicators"
RANGING = ["SIDEWAYS", "CONSOLIDATION", "LOW_VOLATILITY"]


def _vol_label(ctx: StrategyContext) -> str:
    return "real volume" if "real_volume" in ctx.data_caps else "broker tick volume"


class RsiObOs(Strategy):
    """RSI crosses back above oversold / below overbought."""
    code, name, category, component = "ind_rsi_obos", "RSI Overbought/Oversold", CAT, C_INDICATOR
    params = {"n": 14, "lo": 30, "hi": 70, "rr": 1.5}
    regimes = RANGING

    def detect(self, ctx):
        r = ctx.rsi(self.p["n"])
        if cross_up(r, float(self.p["lo"])):
            return self.make(ctx, "BUY", float(ctx.df.low.iloc[-5:].min()), reasons=["RSI exits oversold"])
        if cross_down(r, float(self.p["hi"])):
            return self.make(ctx, "SELL", float(ctx.df.high.iloc[-5:].max()), reasons=["RSI exits overbought"])
        return None


def _divergence(ctx: StrategyContext, osc: pd.Series, strength: int = 3):
    """Regular divergence between the last two confirmed swings; fires when the latest swing confirms."""
    sw = ctx.known_swings(strength)
    lows, highs = [s for s in sw if s.kind == "L"][-2:], [s for s in sw if s.kind == "H"][-2:]
    if len(lows) == 2 and lows[1].confirmed == ctx.n - 1:
        a, b = lows
        if b.price < a.price and osc.iloc[b.idx] > osc.iloc[a.idx]:
            return "BUY", b.price
    if len(highs) == 2 and highs[1].confirmed == ctx.n - 1:
        a, b = highs
        if b.price > a.price and osc.iloc[b.idx] < osc.iloc[a.idx]:
            return "SELL", b.price
    return None


class RsiDivergence(Strategy):
    """Price makes lower low / higher high while RSI does not."""
    code, name, category, component = "ind_rsi_divergence", "RSI Divergence", CAT, C_INDICATOR

    def detect(self, ctx):
        d = _divergence(ctx, ctx.rsi())
        return self.make(ctx, d[0], d[1], strength=0.6, reasons=[f"RSI {d[0]} divergence"]) if d else None


class MacdCrossover(Strategy):
    """MACD crosses its signal line on the side of zero that favours mean reversion of momentum."""
    code, name, category, component = "ind_macd_cross", "MACD Crossover", CAT, C_INDICATOR

    def detect(self, ctx):
        m = ctx.macd()
        if cross_up(m.macd, m.signal) and m.macd.iloc[-1] < 0:
            return self.make(ctx, "BUY", None, reasons=["MACD bull cross below zero"])
        if cross_down(m.macd, m.signal) and m.macd.iloc[-1] > 0:
            return self.make(ctx, "SELL", None, reasons=["MACD bear cross above zero"])
        return None


class MacdDivergence(Strategy):
    """Price/MACD-histogram regular divergence at confirmed swings."""
    code, name, category, component = "ind_macd_divergence", "MACD Divergence", CAT, C_INDICATOR

    def detect(self, ctx):
        d = _divergence(ctx, ctx.macd().macd)
        return self.make(ctx, d[0], d[1], strength=0.6, reasons=[f"MACD {d[0]} divergence"]) if d else None


class Stochastic(Strategy):
    """%K crosses %D inside the oversold / overbought zone."""
    code, name, category, component = "ind_stochastic", "Stochastic Oscillator", CAT, C_INDICATOR
    params = {"lo": 20, "hi": 80, "rr": 1.5}
    regimes = RANGING

    def detect(self, ctx):
        s = ctx.memo(("stoch",), lambda: ind.stochastic(ctx.df))
        if cross_up(s.k, s.d) and s.k.iloc[-2] < self.p["lo"]:
            return self.make(ctx, "BUY", None, reasons=["Stochastic bull cross in oversold"])
        if cross_down(s.k, s.d) and s.k.iloc[-2] > self.p["hi"]:
            return self.make(ctx, "SELL", None, reasons=["Stochastic bear cross in overbought"])
        return None


class BollingerBands(Strategy):
    """Band-walk continuation: close outside the band while ADX > 20 and rising."""
    code, name, category, component = "ind_bollinger", "Bollinger Bands", CAT, C_INDICATOR
    regimes = ["STRONG_BULL", "STRONG_BEAR", "BREAKOUT"]

    def detect(self, ctx):
        b, a = ctx.bb().iloc[-1], ctx.adx().adx
        c = ctx.last()
        if a.iloc[-1] < 20 or a.iloc[-1] <= a.iloc[-2]:
            return None
        if c > b.upper and ctx.df.close.iloc[-2] <= ctx.bb().upper.iloc[-2]:
            return self.make(ctx, "BUY", float(b.mid), reasons=["Close above upper band, ADX rising"])
        if c < b.lower and ctx.df.close.iloc[-2] >= ctx.bb().lower.iloc[-2]:
            return self.make(ctx, "SELL", float(b.mid), reasons=["Close below lower band, ADX rising"])
        return None


class BollingerSqueeze(Strategy):
    """TTM-style squeeze (BB inside Keltner) releasing with momentum."""
    code, name, category, component = "ind_bb_squeeze", "Bollinger Squeeze", CAT, C_INDICATOR
    regimes = ["CONSOLIDATION", "LOW_VOLATILITY", "BREAKOUT"]

    def detect(self, ctx):
        b, k = ctx.bb(), ctx.memo(("kc",), lambda: ind.keltner(ctx.df))
        sq = (b.upper < k.upper) & (b.lower > k.lower)
        if not (sq.iloc[-2] and not sq.iloc[-1]):
            return None
        mom = ind.linreg_slope(ctx.close - (ctx.df.high.rolling(20).max() + ctx.df.low.rolling(20).min()) / 2, 20)
        if mom.iloc[-1] > 0:
            return self.make(ctx, "BUY", float(b.mid.iloc[-1]), reasons=["Squeeze fired up"])
        if mom.iloc[-1] < 0:
            return self.make(ctx, "SELL", float(b.mid.iloc[-1]), reasons=["Squeeze fired down"])
        return None


class KeltnerChannels(Strategy):
    """Close outside the Keltner channel after being inside — volatility expansion."""
    code, name, category, component = "ind_keltner", "Keltner Channels", CAT, C_INDICATOR

    def detect(self, ctx):
        k = ctx.memo(("kc",), lambda: ind.keltner(ctx.df))
        if cross_up(ctx.close, k.upper):
            return self.make(ctx, "BUY", float(k.mid.iloc[-1]), reasons=["Keltner upper breakout"])
        if cross_down(ctx.close, k.lower):
            return self.make(ctx, "SELL", float(k.mid.iloc[-1]), reasons=["Keltner lower breakdown"])
        return None


class AtrBreakout(Strategy):
    """Close beyond previous close +/- k*ATR."""
    code, name, category, component = "ind_atr_breakout", "ATR Breakout", CAT, C_INDICATOR
    params = {"k": 1.5, "sl_atr": 1.0, "rr": 2.0}
    regimes = ["BREAKOUT", "HIGH_VOLATILITY", "STRONG_BULL", "STRONG_BEAR"]

    def detect(self, ctx):
        a, pc, c = ctx.atr().iloc[-2], ctx.df.close.iloc[-2], ctx.last()
        if c > pc + self.p["k"] * a:
            return self.make(ctx, "BUY", None, reasons=["ATR expansion up"])
        if c < pc - self.p["k"] * a:
            return self.make(ctx, "SELL", None, reasons=["ATR expansion down"])
        return None


class CciStrategy(Strategy):
    """CCI crosses back above -100 / below +100."""
    code, name, category, component = "ind_cci", "CCI Strategy", CAT, C_INDICATOR
    regimes = RANGING

    def detect(self, ctx):
        c = ctx.memo(("cci",), lambda: ind.cci(ctx.df))
        if cross_up(c, -100.0):
            return self.make(ctx, "BUY", None, reasons=["CCI exits -100"])
        if cross_down(c, 100.0):
            return self.make(ctx, "SELL", None, reasons=["CCI exits +100"])
        return None


class WilliamsR(Strategy):
    """Williams %R crosses -80 upward / -20 downward with EMA50 filter."""
    code, name, category, component = "ind_williams_r", "Williams %R", CAT, C_INDICATOR

    def detect(self, ctx):
        w = ctx.memo(("wr",), lambda: ind.williams_r(ctx.df))
        e = ctx.ema(50).iloc[-1]
        if cross_up(w, -80.0) and ctx.last() > e:
            return self.make(ctx, "BUY", None, reasons=["%R exits oversold in uptrend"])
        if cross_down(w, -20.0) and ctx.last() < e:
            return self.make(ctx, "SELL", None, reasons=["%R exits overbought in downtrend"])
        return None


class MfiStrategy(Strategy):
    """Money Flow Index crosses 20 up / 80 down (volume = broker tick volume on OTC)."""
    code, name, category, component = "ind_mfi", "MFI Strategy", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        m = ctx.memo(("mfi",), lambda: ind.mfi(ctx.df))
        if cross_up(m, 20.0):
            return self.make(ctx, "BUY", None, reasons=[f"MFI exits oversold ({_vol_label(ctx)})"])
        if cross_down(m, 80.0):
            return self.make(ctx, "SELL", None, reasons=[f"MFI exits overbought ({_vol_label(ctx)})"])
        return None


class ObvStrategy(Strategy):
    """OBV makes a 20-bar high/low before price (accumulation/distribution lead)."""
    code, name, category, component = "ind_obv", "OBV Strategy", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        o = ctx.memo(("obv",), lambda: ind.obv(ctx.df))
        c = ctx.close
        if o.iloc[-1] > o.iloc[-21:-1].max() and c.iloc[-1] < c.iloc[-21:-1].max() and c.iloc[-1] > ctx.ema(50).iloc[-1]:
            return self.make(ctx, "BUY", None, reasons=[f"OBV leads higher ({_vol_label(ctx)})"])
        if o.iloc[-1] < o.iloc[-21:-1].min() and c.iloc[-1] > c.iloc[-21:-1].min() and c.iloc[-1] < ctx.ema(50).iloc[-1]:
            return self.make(ctx, "SELL", None, reasons=[f"OBV leads lower ({_vol_label(ctx)})"])
        return None


class VwapStrategy(Strategy):
    """Reclaim / loss of the session VWAP in the direction of EMA50."""
    code, name, category, component = "ind_vwap", "VWAP Strategy", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]
    timeframes = ["M5", "M15", "M30"]

    def detect(self, ctx):
        v = ctx.memo(("vwap",), lambda: ind.vwap_session(ctx.df, pd.Series(ss.ny_trading_day(ctx.df.index), index=ctx.df.index)))
        up = ctx.ema(50).iloc[-1] > ctx.ema(50).iloc[-5]
        if cross_up(ctx.close, v) and up:
            return self.make(ctx, "BUY", None, reasons=[f"VWAP reclaim ({_vol_label(ctx)})"])
        if cross_down(ctx.close, v) and not up:
            return self.make(ctx, "SELL", None, reasons=[f"VWAP loss ({_vol_label(ctx)})"])
        return None


class AnchoredVwap(Strategy):
    """AVWAP anchored at the last major swing; trend pullback that holds the AVWAP."""
    code, name, category, component = "ind_anchored_vwap", "Anchored VWAP", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        trend, c = ctx.events()[1], candle(ctx)
        s = last_swing(ctx, "L" if trend > 0 else "H", 5)
        if not s or trend == 0:
            return None
        av = ind.anchored_vwap(ctx.df, s.idx).iloc[-1]
        if trend > 0 and c["l"] <= av < c["c"] and c["bull"]:
            return self.make(ctx, "BUY", s.price, reasons=["AVWAP from swing low held"])
        if trend < 0 and c["h"] >= av > c["c"] and c["bear"]:
            return self.make(ctx, "SELL", s.price, reasons=["AVWAP from swing high held"])
        return None


def last_leg(ctx: StrategyContext, strength: int = 3):
    sw = ctx.known_swings(strength)
    h = [s for s in sw if s.kind == "H"]
    l = [s for s in sw if s.kind == "L"]
    if not h or not l:
        return None
    return h[-1], l[-1]


class FibRetracement(Strategy):
    """Rejection inside the 38.2-61.8% retracement of the latest swing leg in trend direction."""
    code, name, category, component = "ind_fib_retracement", "Fibonacci Retracement", CAT, C_INDICATOR
    params = {"lo": 0.382, "hi": 0.618}

    def detect(self, ctx):
        leg = last_leg(ctx)
        if not leg:
            return None
        h, l = leg
        c, rng = candle(ctx), h.price - l.price
        if rng <= 0:
            return None
        if l.idx < h.idx:  # up leg
            top, bot = h.price - self.p["lo"] * rng, h.price - self.p["hi"] * rng
            if bot <= c["l"] <= top and c["c"] > top and c["bull"]:
                return self.make(ctx, "BUY", l.price, target=h.price + 0.272 * rng, reasons=["Fib 38-62% hold"])
        else:
            bot, top = l.price + self.p["lo"] * rng, l.price + self.p["hi"] * rng
            if bot <= c["h"] <= top and c["c"] < bot and c["bear"]:
                return self.make(ctx, "SELL", h.price, target=l.price - 0.272 * rng, reasons=["Fib 38-62% reject"])
        return None


class FibExtension(Strategy):
    """BOS continuation targeting the 1.618 extension of the prior leg."""
    code, name, category, component = "ind_fib_extension", "Fibonacci Extension", CAT, C_INDICATOR
    params = {"ext": 1.618}

    def detect(self, ctx):
        ev, _ = ctx.events()
        if not ev or ev[-1].idx != ctx.n - 1 or ev[-1].kind != "BOS":
            return None
        leg = last_leg(ctx)
        if not leg:
            return None
        h, l = leg
        rng = h.price - l.price
        if ev[-1].direction == "BUY" and rng > 0:
            return self.make(ctx, "BUY", l.price, target=l.price + self.p["ext"] * rng, reasons=["Fib 1.618 extension target"])
        if ev[-1].direction == "SELL" and rng > 0:
            return self.make(ctx, "SELL", h.price, target=h.price - self.p["ext"] * rng, reasons=["Fib 1.618 extension target"])
        return None


def _prev_day(ctx):
    return ctx.memo(("pdl",), lambda: st.period_levels(ctx.df, "D")).iloc[-1]


class PivotPoints(Strategy):
    """Rejection at classic daily S1/R1 (or S2/R2) pivots."""
    code, name, category, component = "ind_pivots", "Pivot Points", CAT, C_INDICATOR
    timeframes = ["M15", "M30", "H1"]

    def detect(self, ctx):
        p = _prev_day(ctx)
        if np.isnan(p.get("close", np.nan)):
            return None
        pv, c = ind.pivots_classic(p.high, p.low, p.close), candle(ctx)
        for s in ("S1", "S2"):
            if c["l"] <= pv[s] < c["c"] and c["bull"]:
                return self.make(ctx, "BUY", c["l"], target=pv["P"] if pv["P"] > c["c"] else None, reasons=[f"Pivot {s} bounce"])
        for r in ("R1", "R2"):
            if c["h"] >= pv[r] > c["c"] and c["bear"]:
                return self.make(ctx, "SELL", c["h"], target=pv["P"] if pv["P"] < c["c"] else None, reasons=[f"Pivot {r} rejection"])
        return None


class Camarilla(Strategy):
    """Fade at Camarilla H3/L3; breakout continuation through H4/L4."""
    code, name, category, component = "ind_camarilla", "Camarilla Pivot", CAT, C_INDICATOR
    timeframes = ["M5", "M15", "M30"]

    def detect(self, ctx):
        p = _prev_day(ctx)
        if np.isnan(p.get("close", np.nan)):
            return None
        cm, c, prev = ind.pivots_camarilla(p.high, p.low, p.close), candle(ctx), ctx.df.close.iloc[-2]
        if prev <= cm["R4"] < c["c"]:
            return self.make(ctx, "BUY", cm["R3"], reasons=["Camarilla R4 breakout"])
        if prev >= cm["S4"] > c["c"]:
            return self.make(ctx, "SELL", cm["S3"], reasons=["Camarilla S4 breakdown"])
        if c["h"] >= cm["R3"] > c["c"] and c["bear"]:
            return self.make(ctx, "SELL", cm["R4"], reasons=["Camarilla R3 fade"])
        if c["l"] <= cm["S3"] < c["c"] and c["bull"]:
            return self.make(ctx, "BUY", cm["S4"], reasons=["Camarilla S3 fade"])
        return None


class VolumeProfile(Strategy):
    """Rejection at the value-area edge of the last 100 bars, target the POC."""
    code, name, category, component = "ind_volume_profile", "Volume Profile", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]
    params = {"lookback": 100}

    def detect(self, ctx):
        vp = ind.volume_profile(ctx.df.iloc[-self.p["lookback"] - 1:-1])
        if vp["poc"] is None:
            return None
        c = candle(ctx)
        if c["l"] <= vp["val"] < c["c"] and c["bull"]:
            return self.make(ctx, "BUY", c["l"], target=vp["poc"], reasons=[f"VAL rejection ({_vol_label(ctx)})"])
        if c["h"] >= vp["vah"] > c["c"] and c["bear"]:
            return self.make(ctx, "SELL", c["h"], target=vp["poc"], reasons=[f"VAH rejection ({_vol_label(ctx)})"])
        return None


class MarketProfile(Strategy):
    """80% rule on a time-at-price (TPO) value area of the previous day."""
    code, name, category, component = "ind_market_profile", "Market Profile", CAT, C_INDICATOR
    timeframes = ["M15", "M30"]
    min_bars = 150

    def detect(self, ctx):
        day = ss.ny_trading_day(ctx.df.index)
        days = pd.unique(day)
        if len(days) < 2:
            return None
        prev = ctx.df[day == days[-2]]
        today = ctx.df[day == days[-1]]
        if len(prev) < 10 or len(today) < 3:
            return None
        tpo = prev.assign(volume=1)  # one TPO per bar => time-at-price profile
        vp = ind.volume_profile(tpo, bins=20)
        o, c = float(today.open.iloc[0]), candle(ctx)
        prior = today.close.iloc[:-1]
        if o < vp["val"] and (prior < vp["val"]).all() and c["c"] > vp["val"]:
            return self.make(ctx, "BUY", float(today.low.min()), target=vp["vah"], reasons=["80% rule: re-entry into VA from below"])
        if o > vp["vah"] and (prior > vp["vah"]).all() and c["c"] < vp["vah"]:
            return self.make(ctx, "SELL", float(today.high.max()), target=vp["val"], reasons=["80% rule: re-entry into VA from above"])
        return None


class DonchianBreakout(Strategy):
    """Classic 20-bar Donchian channel breakout."""
    code, name, category, component = "ind_donchian_breakout", "Donchian Channel Breakout", CAT, C_INDICATOR
    params = {"n": 20, "rr": 2.0}

    def detect(self, ctx):
        d, c, pc = ind.donchian(ctx.df, self.p["n"]), ctx.last(), ctx.df.close.iloc[-2]
        if np.isnan(d.upper.iloc[-1]):
            return None
        if c > d.upper.iloc[-1] and pc <= d.upper.iloc[-2]:
            return self.make(ctx, "BUY", float(d.mid.iloc[-1]), reasons=["Donchian-20 breakout"])
        if c < d.lower.iloc[-1] and pc >= d.lower.iloc[-2]:
            return self.make(ctx, "SELL", float(d.mid.iloc[-1]), reasons=["Donchian-20 breakdown"])
        return None


class AroonStrategy(Strategy):
    """Aroon Up crosses Aroon Down with the leading line above 70."""
    code, name, category, component = "ind_aroon", "Aroon Indicator", CAT, C_INDICATOR

    def detect(self, ctx):
        a = ctx.memo(("aroon",), lambda: ind.aroon(ctx.df))
        if cross_up(a.up, a.down) and a.up.iloc[-1] >= 70:
            return self.make(ctx, "BUY", None, reasons=["Aroon bullish cross"])
        if cross_up(a.down, a.up) and a.down.iloc[-1] >= 70:
            return self.make(ctx, "SELL", None, reasons=["Aroon bearish cross"])
        return None


class TsiStrategy(Strategy):
    """True Strength Index crosses its signal line on the far side of zero."""
    code, name, category, component = "ind_tsi", "TSI Strategy", CAT, C_INDICATOR

    def detect(self, ctx):
        t = ctx.memo(("tsi",), lambda: ind.tsi(ctx.close))
        if cross_up(t.tsi, t.signal) and t.tsi.iloc[-1] < 0:
            return self.make(ctx, "BUY", None, reasons=["TSI bull cross below zero"])
        if cross_down(t.tsi, t.signal) and t.tsi.iloc[-1] > 0:
            return self.make(ctx, "SELL", None, reasons=["TSI bear cross above zero"])
        return None


class ElderRay(Strategy):
    """Rising EMA13 with negative-but-rising bear power (buy) and the mirror for sells."""
    code, name, category, component = "ind_elder_ray", "Elder Ray Strategy", CAT, C_INDICATOR

    def detect(self, ctx):
        e = ctx.memo(("elder",), lambda: ind.elder_ray(ctx.df))
        up = e.ema.iloc[-1] > e.ema.iloc[-2]
        if up and e.bear.iloc[-1] < 0 and e.bear.iloc[-1] > e.bear.iloc[-2] and e.bear.iloc[-2] <= e.bear.iloc[-3]:
            return self.make(ctx, "BUY", None, reasons=["Elder ray: bear power turning up in uptrend"])
        if not up and e.bull.iloc[-1] > 0 and e.bull.iloc[-1] < e.bull.iloc[-2] and e.bull.iloc[-2] >= e.bull.iloc[-3]:
            return self.make(ctx, "SELL", None, reasons=["Elder ray: bull power turning down in downtrend"])
        return None


STRATEGIES = [RsiObOs, RsiDivergence, MacdCrossover, MacdDivergence, Stochastic, BollingerBands, BollingerSqueeze,
              KeltnerChannels, AtrBreakout, CciStrategy, WilliamsR, MfiStrategy, ObvStrategy, VwapStrategy,
              AnchoredVwap, FibRetracement, FibExtension, PivotPoints, Camarilla, VolumeProfile, MarketProfile,
              DonchianBreakout, AroonStrategy, TsiStrategy, ElderRay]
