"""Price action strategies (30): levels, chart patterns and candlestick setups."""
from __future__ import annotations

import numpy as np

from app.analysis import indicators as ind
from app.analysis import structure as st
from app.strategies.base import (C_PRICE_ACTION, C_STRUCTURE, C_TREND, Strategy, StrategyContext, candle, cross_up,
                                 cross_down, last_swing)

CAT = "price_action"


def levels(ctx: StrategyContext, strength: int = 3, tol_atr: float = 0.25, min_touches: int = 2) -> list[tuple[float, int]]:
    """Horizontal S/R: cluster known swing prices within tol; returns (price, touches)."""
    def build():
        tol = tol_atr * ctx.atr_last()
        prices = sorted(s.price for s in ctx.known_swings(strength))
        clusters: list[list[float]] = []
        for p in prices:
            if clusters and p - clusters[-1][-1] <= tol:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        return [(float(np.mean(c)), len(c)) for c in clusters if len(c) >= min_touches]
    return ctx.memo(("levels", strength, tol_atr, min_touches), build)


def line_at(a: st.Swing, b: st.Swing, idx: int) -> float:
    return a.price + (b.price - a.price) * (idx - a.idx) / (b.idx - a.idx)


def last_n(ctx, kind, n, strength=3):
    sw = [s for s in ctx.known_swings(strength) if s.kind == kind]
    return sw[-n:] if len(sw) >= n else None


class SupportResistance(Strategy):
    """Rejection at a multi-touch horizontal level."""
    code, name, category, component = "pa_support_resistance", "Support and Resistance", CAT, C_PRICE_ACTION
    params = {"strength": 3, "tol_atr": 0.25, "rr": 2.0}

    def detect(self, ctx):
        c, tol = candle(ctx), 0.25 * ctx.atr_last()
        for lvl, touches in levels(ctx, self.p["strength"], self.p["tol_atr"]):
            s = min(0.4 + 0.1 * touches, 0.8)
            if c["l"] <= lvl + tol and c["l"] >= lvl - 2 * tol and c["c"] > lvl and c["bull"]:
                return self.make(ctx, "BUY", min(c["l"], lvl), strength=s, reasons=[f"Support {lvl:.5f} x{touches}"])
            if c["h"] >= lvl - tol and c["h"] <= lvl + 2 * tol and c["c"] < lvl and c["bear"]:
                return self.make(ctx, "SELL", max(c["h"], lvl), strength=s, reasons=[f"Resistance {lvl:.5f} x{touches}"])
        return None


class SupplyDemand(Strategy):
    """Base of 1-4 small candles followed by a displacement departure; first return to the base."""
    code, name, category, component = "pa_supply_demand", "Supply and Demand", CAT, C_PRICE_ACTION
    params = {"max_base": 4, "depart_mult": 1.8, "max_age": 60, "rr": 2.5}

    def detect(self, ctx):
        df, i, c = ctx.df, ctx.n - 1, candle(ctx)
        o, cl, h, l = (df[k].to_numpy(float) for k in ("open", "close", "high", "low"))
        a = ctx.atr().to_numpy(float)
        for k in range(i - 2, max(i - self.p["max_age"], 20), -1):
            body = cl[k] - o[k]
            if np.isnan(a[k]) or abs(body) < self.p["depart_mult"] * a[k]:
                continue
            j = k - 1
            while j > k - 1 - self.p["max_base"] and abs(cl[j] - o[j]) < 0.5 * a[k]:
                j -= 1
            if j == k - 1:
                continue
            zt, zb = float(h[j + 1:k].max()), float(l[j + 1:k].min())
            later = slice(k + 1, i)
            if body > 0:  # demand
                if l[later].size and l[later].min() <= zt:
                    continue  # not first return
                if c["l"] <= zt and c["c"] > zb and c["bull"]:
                    return self.make(ctx, "BUY", zb, strength=0.6, reasons=[f"Demand zone {zb:.5f}-{zt:.5f}"])
            else:
                if h[later].size and h[later].max() >= zb:
                    continue
                if c["h"] >= zb and c["c"] < zt and c["bear"]:
                    return self.make(ctx, "SELL", zt, strength=0.6, reasons=[f"Supply zone {zb:.5f}-{zt:.5f}"])
        return None


class TrendlineTrading(Strategy):
    """Bounce from a rising (falling) trendline drawn through the last two swing lows (highs)."""
    code, name, category, component = "pa_trendline", "Trendline Trading", CAT, C_TREND
    params = {"strength": 3, "tol_atr": 0.3, "rr": 2.0}

    def detect(self, ctx):
        i, c, tol = ctx.n - 1, candle(ctx), self.p["tol_atr"] * ctx.atr_last()
        lows = last_n(ctx, "L", 2, self.p["strength"])
        if lows and lows[1].price > lows[0].price:
            y = line_at(lows[0], lows[1], i)
            if abs(c["l"] - y) <= tol and c["c"] > y and c["bull"]:
                return self.make(ctx, "BUY", y, reasons=["Rising trendline support"])
        highs = last_n(ctx, "H", 2, self.p["strength"])
        if highs and highs[1].price < highs[0].price:
            y = line_at(highs[0], highs[1], i)
            if abs(c["h"] - y) <= tol and c["c"] < y and c["bear"]:
                return self.make(ctx, "SELL", y, reasons=["Falling trendline resistance"])
        return None


class BreakoutTrading(Strategy):
    """Close beyond an N-bar range that was compressed (range < k * ATR)."""
    code, name, category, component = "pa_breakout", "Breakout Trading", CAT, C_PRICE_ACTION
    params = {"lookback": 20, "max_range_atr": 4.0, "rr": 2.0}
    regimes = ["CONSOLIDATION", "BREAKOUT", "LOW_VOLATILITY"]

    def detect(self, ctx):
        prev = ctx.df.iloc[-self.p["lookback"] - 1:-1]
        hi, lo, c = float(prev.high.max()), float(prev.low.min()), candle(ctx)
        if hi - lo > self.p["max_range_atr"] * ctx.atr_last():
            return None
        if c["c"] > hi:
            return self.make(ctx, "BUY", (hi + lo) / 2, reasons=["Compressed range breakout up"])
        if c["c"] < lo:
            return self.make(ctx, "SELL", (hi + lo) / 2, reasons=["Compressed range breakdown"])
        return None


class RetestTrading(Strategy):
    """Horizontal S/R level broken within the last N bars and now retested from the other side."""
    code, name, category, component = "pa_retest", "Retest Trading", CAT, C_PRICE_ACTION
    params = {"window": 15, "rr": 2.0}

    def detect(self, ctx):
        c, w = candle(ctx), self.p["window"]
        closes = ctx.df.close.iloc[-w - 1:-1].to_numpy(float)
        for lvl, _ in levels(ctx):
            above = closes > lvl
            if not above[0] and above[-1] and c["l"] <= lvl < c["c"]:
                return self.make(ctx, "BUY", lvl, reasons=[f"Broken resistance {lvl:.5f} retested as support"])
            if above[0] and not above[-1] and c["h"] >= lvl > c["c"]:
                return self.make(ctx, "SELL", lvl, reasons=[f"Broken support {lvl:.5f} retested as resistance"])
        return None


class PullbackTrading(Strategy):
    """EMA20 > EMA50 trend; pullback touching EMA20 with a trend-direction close."""
    code, name, category, component = "pa_pullback", "Pullback Trading", CAT, C_TREND
    params = {"fast": 20, "slow": 50, "rr": 2.0}
    regimes = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR"]

    def detect(self, ctx):
        f, s, c = ctx.ema(self.p["fast"]).iloc[-1], ctx.ema(self.p["slow"]).iloc[-1], candle(ctx)
        if np.isnan(s):
            return None
        if f > s and c["l"] <= f and c["c"] > f and c["bull"]:
            return self.make(ctx, "BUY", min(c["l"], s), reasons=["Pullback to EMA20 in uptrend"])
        if f < s and c["h"] >= f and c["c"] < f and c["bear"]:
            return self.make(ctx, "SELL", max(c["h"], s), reasons=["Pullback to EMA20 in downtrend"])
        return None


def _engulf(ctx, k=1):
    a, b = candle(ctx, k + 1), candle(ctx, k)
    if b["bull"] and a["bear"] and b["c"] >= a["o"] and b["o"] <= a["c"]:
        return "BUY"
    if b["bear"] and a["bull"] and b["c"] <= a["o"] and b["o"] >= a["c"]:
        return "SELL"
    return None


def _pin(ctx, ratio=0.6):
    c = candle(ctx)
    if c["lower"] / c["range"] >= ratio and c["body"] / c["range"] <= 0.35:
        return "BUY"
    if c["upper"] / c["range"] >= ratio and c["body"] / c["range"] <= 0.35:
        return "SELL"
    return None


class ReversalTrading(Strategy):
    """Over-extension (> k ATR from EMA50) followed by an engulfing or pin reversal candle."""
    code, name, category, component = "pa_reversal", "Reversal Trading", CAT, C_PRICE_ACTION
    params = {"ext_atr": 3.0, "rr": 2.0}
    regimes = ["REVERSAL", "HIGH_VOLATILITY", "SIDEWAYS"]

    def detect(self, ctx):
        e, a, c = ctx.ema(50).iloc[-2], ctx.atr_last(), candle(ctx)
        sig = _engulf(ctx) or _pin(ctx)
        prev_c = ctx.df.close.iloc[-2]
        if sig == "BUY" and e - prev_c > self.p["ext_atr"] * a:
            return self.make(ctx, "BUY", c["l"], target=float(e), reasons=["Extended below EMA50 + reversal candle"])
        if sig == "SELL" and prev_c - e > self.p["ext_atr"] * a:
            return self.make(ctx, "SELL", c["h"], target=float(e), reasons=["Extended above EMA50 + reversal candle"])
        return None


class RangeTrading(Strategy):
    """ADX < 20; rejection at the 50-bar Donchian edges, target the midline."""
    code, name, category, component = "pa_range", "Range Trading", CAT, C_PRICE_ACTION
    params = {"n": 50, "adx_max": 20, "edge": 0.1}
    regimes = ["SIDEWAYS", "CONSOLIDATION", "LOW_VOLATILITY"]

    def detect(self, ctx):
        if ctx.adx().adx.iloc[-1] >= self.p["adx_max"]:
            return None
        d = ind.donchian(ctx.df, self.p["n"]).iloc[-1]
        hi, lo, c = d.upper, d.lower, candle(ctx)
        if np.isnan(hi):
            return None
        band = self.p["edge"] * (hi - lo)
        if c["l"] <= lo + band and c["c"] > lo and c["bull"]:
            return self.make(ctx, "BUY", lo, target=float(d.mid), reasons=["Range low rejection"])
        if c["h"] >= hi - band and c["c"] < hi and c["bear"]:
            return self.make(ctx, "SELL", hi, target=float(d.mid), reasons=["Range high rejection"])
        return None


def _flag(ctx, direction, pole_bars=8, pole_atr=3.0, flag_min=3, flag_max=12, max_retrace=0.5):
    df, a = ctx.df, ctx.atr_last()
    c = candle(ctx)
    for fl in range(flag_min, flag_max + 1):
        flag = df.iloc[-fl - 1:-1]
        pole = df.iloc[-fl - 1 - pole_bars:-fl - 1]
        if len(pole) < pole_bars:
            return None
        if direction == "BUY":
            move = pole.close.iloc[-1] - pole.low.min()
            if move < pole_atr * a:
                continue
            retr = (pole.high.max() - flag.low.min()) / move
            if retr <= max_retrace and flag.high.max() <= pole.high.max() * 1.0005 and c["c"] > flag.high.max():
                return float(flag.low.min()), float(move)
        else:
            move = pole.high.max() - pole.close.iloc[-1]
            if move < pole_atr * a:
                continue
            retr = (flag.high.max() - pole.low.min()) / move
            if retr <= max_retrace and flag.low.min() >= pole.low.min() * 0.9995 and c["c"] < flag.low.min():
                return float(flag.high.max()), float(move)
    return None


class FlagPattern(Strategy):
    """Pole + shallow consolidation; breakout in pole direction (either side)."""
    code, name, category, component = "pa_flag", "Flag Pattern", CAT, C_PRICE_ACTION
    params = {"pole_atr": 3.0}

    def detect(self, ctx):
        for d in ("BUY", "SELL"):
            r = _flag(ctx, d, pole_atr=self.p["pole_atr"])
            if r:
                stop, pole = r
                e = ctx.last()
                return self.make(ctx, d, stop, target=e + pole if d == "BUY" else e - pole, reasons=[f"{d} flag breakout"])
        return None


class BullFlag(Strategy):
    """Bullish flag: strong rally, shallow pullback channel, breakout above the flag."""
    code, name, category, component = "pa_bull_flag", "Bull Flag", CAT, C_PRICE_ACTION
    params = {"pole_atr": 3.0}

    def detect(self, ctx):
        r = _flag(ctx, "BUY", pole_atr=self.p["pole_atr"])
        return self.make(ctx, "BUY", r[0], target=ctx.last() + r[1], reasons=["Bull flag breakout"]) if r else None


class BearFlag(Strategy):
    """Bearish flag: strong drop, shallow bounce, breakdown below the flag."""
    code, name, category, component = "pa_bear_flag", "Bear Flag", CAT, C_PRICE_ACTION
    params = {"pole_atr": 3.0}

    def detect(self, ctx):
        r = _flag(ctx, "SELL", pole_atr=self.p["pole_atr"])
        return self.make(ctx, "SELL", r[0], target=ctx.last() - r[1], reasons=["Bear flag breakdown"]) if r else None


class HeadShoulders(Strategy):
    """Three highs (head highest, shoulders within tolerance); close below the neckline."""
    code, name, category, component = "pa_head_shoulders", "Head and Shoulders", CAT, C_PRICE_ACTION
    params = {"strength": 3, "sh_tol_atr": 1.0, "max_sl_atr": 10.0}
    inverse = False

    def detect(self, ctx):
        k1, k2 = ("L", "H") if self.inverse else ("H", "L")
        pts = last_n(ctx, k1, 3, self.p["strength"])
        if not pts:
            return None
        ls, hd, rs = pts
        tol = self.p["sh_tol_atr"] * ctx.atr_last()
        sgn = -1 if self.inverse else 1
        if not (sgn * (hd.price - ls.price) > 0 and sgn * (hd.price - rs.price) > 0 and abs(ls.price - rs.price) <= tol):
            return None
        troughs = [s for s in ctx.known_swings(self.p["strength"]) if s.kind == k2 and ls.idx < s.idx < rs.idx]
        if not troughs:
            return None
        neck = max(t.price for t in troughs) if self.inverse else min(t.price for t in troughs)
        c, prev = candle(ctx), ctx.df.close.iloc[-2]
        height = abs(hd.price - neck)
        if not self.inverse and prev >= neck > c["c"]:
            return self.make(ctx, "SELL", rs.price, target=neck - height, strength=0.7, reasons=["H&S neckline break"])
        if self.inverse and prev <= neck < c["c"]:
            return self.make(ctx, "BUY", rs.price, target=neck + height, strength=0.7, reasons=["Inverse H&S neckline break"])
        return None


class InverseHeadShoulders(HeadShoulders):
    """Three lows (head lowest); close above the neckline."""
    code, name = "pa_inverse_head_shoulders", "Inverse Head and Shoulders"
    inverse = True


class _MultiTop(Strategy):
    count, kind = 2, "H"
    params = {"strength": 3, "tol_atr": 0.4, "min_gap": 5, "max_sl_atr": 10.0}

    def detect(self, ctx):
        pts = last_n(ctx, self.kind, self.count, self.p["strength"])
        if not pts:
            return None
        tol = self.p["tol_atr"] * ctx.atr_last()
        prices = [p.price for p in pts]
        if max(prices) - min(prices) > tol or any(b.idx - a.idx < self.p["min_gap"] for a, b in zip(pts, pts[1:])):
            return None
        opp = "L" if self.kind == "H" else "H"
        mids = [s.price for s in ctx.known_swings(self.p["strength"]) if s.kind == opp and pts[0].idx < s.idx < pts[-1].idx]
        if not mids:
            return None
        c, prev = candle(ctx), ctx.df.close.iloc[-2]
        if self.kind == "H":
            neck = min(mids)
            if prev >= neck > c["c"]:
                return self.make(ctx, "SELL", max(prices), target=neck - (max(prices) - neck),
                                 reasons=[f"{self.name} confirmed below {neck:.5f}"], strength=0.6 + 0.05 * self.count)
        else:
            neck = max(mids)
            if prev <= neck < c["c"]:
                return self.make(ctx, "BUY", min(prices), target=neck + (neck - min(prices)),
                                 reasons=[f"{self.name} confirmed above {neck:.5f}"], strength=0.6 + 0.05 * self.count)
        return None


class DoubleTop(_MultiTop):
    """Two equal highs, confirmed by a close through the intervening low."""
    code, name, category, component, count, kind = "pa_double_top", "Double Top", CAT, C_PRICE_ACTION, 2, "H"


class DoubleBottom(_MultiTop):
    """Two equal lows, confirmed by a close through the intervening high."""
    code, name, category, component, count, kind = "pa_double_bottom", "Double Bottom", CAT, C_PRICE_ACTION, 2, "L"


class TripleTop(_MultiTop):
    """Three equal highs, confirmed below the lowest intervening low."""
    code, name, category, component, count, kind = "pa_triple_top", "Triple Top", CAT, C_PRICE_ACTION, 3, "H"


class TripleBottom(_MultiTop):
    """Three equal lows, confirmed above the highest intervening high."""
    code, name, category, component, count, kind = "pa_triple_bottom", "Triple Bottom", CAT, C_PRICE_ACTION, 3, "L"


def _triangle(ctx, strength=3, flat_tol=0.4):
    hs, ls = last_n(ctx, "H", 2, strength), last_n(ctx, "L", 2, strength)
    if not hs or not ls:
        return None
    tol = flat_tol * ctx.atr_last()
    dh, dl = hs[1].price - hs[0].price, ls[1].price - ls[0].price
    flat_h, flat_l = abs(dh) <= tol, abs(dl) <= tol
    i = ctx.n - 1
    return {"hs": hs, "ls": ls, "flat_h": flat_h, "flat_l": flat_l, "rising_l": dl > tol, "falling_h": dh < -tol,
            "rising_h": dh > tol, "falling_l": dl < -tol,
            "upper": line_at(hs[0], hs[1], i), "lower": line_at(ls[0], ls[1], i)}


class AscendingTriangle(Strategy):
    """Flat highs + rising lows; close above the flat top."""
    code, name, category, component = "pa_ascending_triangle", "Ascending Triangle", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        t = _triangle(ctx)
        if t and t["flat_h"] and t["rising_l"]:
            top = max(s.price for s in t["hs"])
            if ctx.df.close.iloc[-2] <= top < ctx.last():
                return self.make(ctx, "BUY", t["lower"], reasons=["Ascending triangle breakout"])
        return None


class DescendingTriangle(Strategy):
    """Flat lows + falling highs; close below the flat base."""
    code, name, category, component = "pa_descending_triangle", "Descending Triangle", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        t = _triangle(ctx)
        if t and t["flat_l"] and t["falling_h"]:
            base = min(s.price for s in t["ls"])
            if ctx.df.close.iloc[-2] >= base > ctx.last():
                return self.make(ctx, "SELL", t["upper"], reasons=["Descending triangle breakdown"])
        return None


class SymmetricalTriangle(Strategy):
    """Falling highs + rising lows (converging); trade the close outside the lines."""
    code, name, category, component = "pa_symmetrical_triangle", "Symmetrical Triangle", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        t = _triangle(ctx)
        if not (t and t["falling_h"] and t["rising_l"]) or t["upper"] <= t["lower"]:
            return None
        if ctx.last() > t["upper"]:
            return self.make(ctx, "BUY", t["lower"], reasons=["Symmetrical triangle break up"])
        if ctx.last() < t["lower"]:
            return self.make(ctx, "SELL", t["upper"], reasons=["Symmetrical triangle break down"])
        return None


class WedgePattern(Strategy):
    """Rising wedge (both lines up, converging) breaks down; falling wedge breaks up."""
    code, name, category, component = "pa_wedge", "Wedge Pattern", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        t = _triangle(ctx)
        if not t:
            return None
        hs, ls = t["hs"], t["ls"]
        slope_h = (hs[1].price - hs[0].price) / max(hs[1].idx - hs[0].idx, 1)
        slope_l = (ls[1].price - ls[0].price) / max(ls[1].idx - ls[0].idx, 1)
        c = ctx.last()
        if t["rising_h"] and t["rising_l"] and slope_l > slope_h and c < t["lower"]:
            return self.make(ctx, "SELL", t["upper"], reasons=["Rising wedge breakdown"])
        if t["falling_h"] and t["falling_l"] and slope_h < slope_l and c > t["upper"]:
            return self.make(ctx, "BUY", t["lower"], reasons=["Falling wedge breakout"])
        return None


class ChannelTrading(Strategy):
    """Parallel swing lines (slopes within 30%); trade bounces off the trend-side boundary."""
    code, name, category, component = "pa_channel", "Channel Trading", CAT, C_TREND
    params = {"tol_atr": 0.3, "rr": 2.0}

    def detect(self, ctx):
        t = _triangle(ctx)
        if not t:
            return None
        hs, ls = t["hs"], t["ls"]
        sh = (hs[1].price - hs[0].price) / max(hs[1].idx - hs[0].idx, 1)
        sl = (ls[1].price - ls[0].price) / max(ls[1].idx - ls[0].idx, 1)
        if sh == 0 or sl == 0 or np.sign(sh) != np.sign(sl) or abs(sh - sl) / max(abs(sh), abs(sl)) > 0.3:
            return None
        c, tol = candle(ctx), self.p["tol_atr"] * ctx.atr_last()
        if sl > 0 and abs(c["l"] - t["lower"]) <= tol and c["bull"]:
            return self.make(ctx, "BUY", t["lower"], target=t["upper"], reasons=["Rising channel support"])
        if sh < 0 and abs(c["h"] - t["upper"]) <= tol and c["bear"]:
            return self.make(ctx, "SELL", t["upper"], target=t["lower"], reasons=["Falling channel resistance"])
        return None


class PinBar(Strategy):
    """Pin bar (wick >= 60% of range) at a 20-bar extreme."""
    code, name, category, component = "pa_pin_bar", "Pin Bar Strategy", CAT, C_PRICE_ACTION
    params = {"ratio": 0.6, "lookback": 20}

    def detect(self, ctx):
        d, c = _pin(ctx, self.p["ratio"]), candle(ctx)
        prev = ctx.df.iloc[-self.p["lookback"] - 1:-1]
        if d == "BUY" and c["l"] <= prev.low.min():
            return self.make(ctx, "BUY", c["l"], reasons=["Bullish pin bar at range low"])
        if d == "SELL" and c["h"] >= prev.high.max():
            return self.make(ctx, "SELL", c["h"], reasons=["Bearish pin bar at range high"])
        return None


class Engulfing(Strategy):
    """Engulfing candle after a 3-bar counter move."""
    code, name, category, component = "pa_engulfing", "Engulfing Candle Strategy", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        d = _engulf(ctx)
        if not d:
            return None
        move = ctx.df.close.iloc[-2] - ctx.df.close.iloc[-5]
        c = candle(ctx)
        lo2, hi2 = min(c["l"], candle(ctx, 2)["l"]), max(c["h"], candle(ctx, 2)["h"])
        if d == "BUY" and move < 0:
            return self.make(ctx, "BUY", lo2, reasons=["Bullish engulfing after decline"])
        if d == "SELL" and move > 0:
            return self.make(ctx, "SELL", hi2, reasons=["Bearish engulfing after rally"])
        return None


class InsideBar(Strategy):
    """Break of the mother bar after an inside bar, in EMA50 direction."""
    code, name, category, component = "pa_inside_bar", "Inside Bar Strategy", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        m, ib, c = candle(ctx, 3), candle(ctx, 2), candle(ctx)
        if not (ib["h"] <= m["h"] and ib["l"] >= m["l"]):
            return None
        e = ctx.ema(50).iloc[-1]
        if c["c"] > m["h"] and c["c"] > e:
            return self.make(ctx, "BUY", ib["l"], reasons=["Inside bar breakout up"])
        if c["c"] < m["l"] and c["c"] < e:
            return self.make(ctx, "SELL", ib["h"], reasons=["Inside bar breakout down"])
        return None


class OutsideBar(Strategy):
    """Outside bar that closes in its top/bottom quarter, beyond the prior bar's extreme."""
    code, name, category, component = "pa_outside_bar", "Outside Bar Strategy", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        p, c = candle(ctx, 2), candle(ctx)
        if not (c["h"] > p["h"] and c["l"] < p["l"]):
            return None
        pos = (c["c"] - c["l"]) / c["range"]
        if pos >= 0.75 and c["c"] > p["h"]:
            return self.make(ctx, "BUY", c["l"], reasons=["Bullish outside bar"])
        if pos <= 0.25 and c["c"] < p["l"]:
            return self.make(ctx, "SELL", c["h"], reasons=["Bearish outside bar"])
        return None


class Fakey(Strategy):
    """Inside bar, false break of the mother bar, close back inside -> trade the opposite way."""
    code, name, category, component = "pa_fakey", "Fakey Pattern", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        m, ib, c = candle(ctx, 3), candle(ctx, 2), candle(ctx)
        if not (ib["h"] <= m["h"] and ib["l"] >= m["l"]):
            return None
        if c["h"] > ib["h"] and c["c"] < ib["h"] and c["c"] < m["h"] and c["bear"]:
            return self.make(ctx, "SELL", c["h"], strength=0.6, reasons=["Bearish fakey"])
        if c["l"] < ib["l"] and c["c"] > ib["l"] and c["c"] > m["l"] and c["bull"]:
            return self.make(ctx, "BUY", c["l"], strength=0.6, reasons=["Bullish fakey"])
        return None


class BreakoutRetest(Strategy):
    """Donchian-20 breakout within the last N bars, entry on retest of the broken edge."""
    code, name, category, component = "pa_breakout_retest", "Breakout and Retest", CAT, C_PRICE_ACTION
    params = {"n": 20, "window": 10}

    def detect(self, ctx):
        d = ind.donchian(ctx.df, self.p["n"])
        cl = ctx.df.close
        c = candle(ctx)
        w = self.p["window"]
        for k in range(ctx.n - w, ctx.n - 1):
            up, lo = d.upper.iloc[k], d.lower.iloc[k]
            if np.isnan(up):
                continue
            if cl.iloc[k] > up and (cl.iloc[k + 1:-1] > up).all() and c["l"] <= up < c["c"]:
                return self.make(ctx, "BUY", up, reasons=[f"Retest of breakout level {up:.5f}"])
            if cl.iloc[k] < lo and (cl.iloc[k + 1:-1] < lo).all() and c["h"] >= lo > c["c"]:
                return self.make(ctx, "SELL", lo, reasons=[f"Retest of breakdown level {lo:.5f}"])
        return None


class CandlestickReversal(Strategy):
    """Hammer / shooting star / morning star / evening star with RSI at an extreme."""
    code, name, category, component = "pa_candlestick_reversal", "Candlestick Reversal", CAT, C_PRICE_ACTION
    params = {"rsi_lo": 35, "rsi_hi": 65}

    def detect(self, ctx):
        r = ctx.rsi().iloc[-2]
        c1, c2, c3 = candle(ctx, 3), candle(ctx, 2), candle(ctx)
        morning = c1["bear"] and c2["body"] < 0.3 * c1["body"] and c3["bull"] and c3["c"] > (c1["o"] + c1["c"]) / 2
        evening = c1["bull"] and c2["body"] < 0.3 * c1["body"] and c3["bear"] and c3["c"] < (c1["o"] + c1["c"]) / 2
        pin = _pin(ctx)
        if r <= self.p["rsi_lo"] and (morning or pin == "BUY"):
            return self.make(ctx, "BUY", min(c2["l"], c3["l"]), reasons=["Morning star/hammer at oversold"])
        if r >= self.p["rsi_hi"] and (evening or pin == "SELL"):
            return self.make(ctx, "SELL", max(c2["h"], c3["h"]), reasons=["Evening star/shooting star at overbought"])
        return None


class MomentumCandle(Strategy):
    """Body > 2x average body, close in top/bottom 20%, aligned with EMA50 slope."""
    code, name, category, component = "pa_momentum_candle", "Momentum Candle Strategy", CAT, C_PRICE_ACTION
    params = {"mult": 2.0, "rr": 1.5}

    def detect(self, ctx):
        c = candle(ctx)
        avg = (ctx.df.close - ctx.df.open).abs().iloc[-21:-1].mean()
        e = ctx.ema(50)
        slope = e.iloc[-1] - e.iloc[-4]
        pos = (c["c"] - c["l"]) / c["range"]
        if c["body"] < self.p["mult"] * avg:
            return None
        if c["bull"] and pos >= 0.8 and slope > 0:
            return self.make(ctx, "BUY", c["l"] + 0.25 * c["range"], reasons=["Bullish momentum candle"])
        if c["bear"] and pos <= 0.2 and slope < 0:
            return self.make(ctx, "SELL", c["h"] - 0.25 * c["range"], reasons=["Bearish momentum candle"])
        return None


STRATEGIES = [SupportResistance, SupplyDemand, TrendlineTrading, BreakoutTrading, RetestTrading, PullbackTrading,
              ReversalTrading, RangeTrading, FlagPattern, BullFlag, BearFlag, HeadShoulders, InverseHeadShoulders,
              DoubleTop, DoubleBottom, TripleTop, TripleBottom, AscendingTriangle, DescendingTriangle,
              SymmetricalTriangle, WedgePattern, ChannelTrading, PinBar, Engulfing, InsideBar, OutsideBar, Fakey,
              BreakoutRetest, CandlestickReversal, MomentumCandle]
_ = (cross_up, cross_down, last_swing, C_STRUCTURE)
