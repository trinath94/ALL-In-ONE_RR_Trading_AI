"""ICT trading models (20). Session windows come from app.analysis.sessions (NY/London
local time via zoneinfo, so DST transitions are handled)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis import sessions as ss
from app.analysis import structure as st
from app.strategies.base import (C_LIQUIDITY, C_SESSION, C_SMC_ICT, C_STRUCTURE, CAP_PEERS, Strategy,
                                 StrategyContext, candle, last_swing)

CAT = "ict"
INTRADAY = ["M1", "M5", "M15", "M30", "H1"]


def _intraday(ctx: StrategyContext) -> bool:
    return ctx.timeframe in INTRADAY


def _in(ctx: StrategyContext, name: str, k: int = 1) -> bool:
    return bool(ss.session_mask(ctx.df.index[-k:], name).iloc[-1])


def _asia(ctx: StrategyContext) -> pd.DataFrame:
    return ctx.memo(("asia_rng",), lambda: st.session_range(ctx.df, ss.session_mask(ctx.df.index, "ASIA")))


def _pd_levels(ctx: StrategyContext) -> pd.Series:
    return ctx.memo(("pdl",), lambda: st.period_levels(ctx.df, "D")).iloc[-1]


def _today(ctx: StrategyContext) -> pd.DataFrame:
    day = ss.ny_trading_day(ctx.df.index)
    return ctx.df[day == day[-1]]


class _Ict(Strategy):
    timeframes = ["M5", "M15"]
    min_bars = 150

    def available(self, ctx):
        if not _intraday(ctx):
            return False, "intraday timeframe required"
        return super().available(ctx)


class SilverBullet(_Ict):
    """FVG formed inside an ICT Silver Bullet hour, entered on first retrace."""
    code, name, category, component = "ict_silver_bullet", "ICT Silver Bullet", CAT, C_SMC_ICT
    params = {"rr": 2.0, "windows": ["SB_LONDON", "SB_NY_AM", "SB_NY_PM"]}

    def detect(self, ctx):
        win = next((w for w in self.p["windows"] if _in(ctx, w)), None)
        if not win:
            return None
        mask = ss.session_mask(ctx.df.index, win).values
        i, c = ctx.n - 1, candle(ctx)
        start = i
        while start > 0 and mask[start - 1]:
            start -= 1
        bias = ctx.htf_bias()
        for g in reversed(ctx.fvgs(0.1)):
            if not (start <= g.idx < i) or g.filled_idx not in (None, i):
                continue
            if bias and (1 if g.direction == "BUY" else -1) != bias:
                continue
            if g.direction == "BUY" and c["l"] <= g.top and c["c"] > g.bottom:
                return self.make(ctx, "BUY", g.bottom, strength=0.65, reasons=[f"{win} bullish FVG retrace"])
            if g.direction == "SELL" and c["h"] >= g.bottom and c["c"] < g.top:
                return self.make(ctx, "SELL", g.top, strength=0.65, reasons=[f"{win} bearish FVG retrace"])
        return None


class Unicorn(_Ict):
    """Breaker block overlapping a same-direction FVG; entry on tap of the overlap."""
    code, name, category, component = "ict_unicorn", "ICT Unicorn Model", CAT, C_SMC_ICT
    params = {"max_age": 40, "rr": 2.5}

    def detect(self, ctx):
        i, c = ctx.n - 1, candle(ctx)
        breakers = [z for z in ctx.order_blocks() if z.broken_idx is not None and i - z.broken_idx <= self.p["max_age"]]
        for z in reversed(breakers):
            bdir = "SELL" if z.direction == "BUY" else "BUY"
            for g in ctx.fvgs(0.1):
                if g.direction != bdir or g.idx < z.broken_idx - 3 or g.idx >= i:
                    continue
                top, bot = min(z.top, g.top), max(z.bottom, g.bottom)
                if top <= bot:
                    continue
                if bdir == "BUY" and c["l"] <= top and c["c"] > bot:
                    return self.make(ctx, "BUY", min(z.bottom, g.bottom), strength=0.75, reasons=["Unicorn: breaker+FVG"])
                if bdir == "SELL" and c["h"] >= bot and c["c"] < top:
                    return self.make(ctx, "SELL", max(z.top, g.top), strength=0.75, reasons=["Unicorn: breaker+FVG"])
        return None


class JudasSwing(_Ict):
    """False move beyond the Asian range in the London kill zone, reversal back inside."""
    code, name, category, component = "ict_judas_swing", "ICT Judas Swing", CAT, C_SMC_ICT

    def detect(self, ctx):
        if not _in(ctx, "LONDON_KZ"):
            return None
        a = _asia(ctx).iloc[-1]
        if np.isnan(a.high):
            return None
        today = _today(ctx)
        c = candle(ctx)
        if today.high.max() > a.high and c["c"] < a.high and c["bear"]:
            return self.make(ctx, "SELL", float(today.high.max()), target=float(a.low), reasons=["Judas above Asia high"])
        if today.low.min() < a.low and c["c"] > a.low and c["bull"]:
            return self.make(ctx, "BUY", float(today.low.min()), target=float(a.high), reasons=["Judas below Asia low"])
        return None


def _sweep_mss_fvg(self: Strategy, ctx: StrategyContext, levels: tuple[float, float], label: str):
    i, c = ctx.n - 1, candle(ctx)
    ev, _ = ctx.events(2)
    recent = [e for e in ev if e.kind in ("CHOCH", "BOS") and i - e.idx <= 12]
    if not recent:
        return None
    e = recent[-1]
    hi_lvl, lo_lvl = levels
    win = ctx.df.iloc[-20:]
    for g in reversed(ctx.fvgs(0.15)):
        if g.direction != e.direction or g.idx < e.idx - 3 or g.idx >= i:
            continue
        if e.direction == "BUY" and win.low.min() < lo_lvl and c["l"] <= g.top and c["c"] > g.bottom:
            return self.make(ctx, "BUY", float(win.low.min()), strength=0.75, reasons=[f"{label}: SSL sweep, MSS, FVG"])
        if e.direction == "SELL" and win.high.max() > hi_lvl and c["h"] >= g.bottom and c["c"] < g.top:
            return self.make(ctx, "SELL", float(win.high.max()), strength=0.75, reasons=[f"{label}: BSL sweep, MSS, FVG"])
    return None


class Ict2022(_Ict):
    """Kill-zone sweep of prior day / session liquidity, displacement MSS, FVG entry."""
    code, name, category, component = "ict_2022_model", "ICT 2022 Model", CAT, C_SMC_ICT
    params = {"rr": 3.0}

    def detect(self, ctx):
        if not any(_in(ctx, k) for k in ("LONDON_KZ", "NY_AM_KZ", "NY_PM_KZ")):
            return None
        pl = _pd_levels(ctx)
        if np.isnan(pl.get("high", np.nan)):
            return None
        a = _asia(ctx).iloc[-1]
        hi = np.nanmin([pl.high, a.high]) if not np.isnan(a.high) else pl.high
        lo = np.nanmax([pl.low, a.low]) if not np.isnan(a.low) else pl.low
        return _sweep_mss_fvg(self, ctx, (hi, lo), "2022 model")


class Ict2023(_Ict):
    """HTF-bias aligned inverse-FVG entry after the Asian range is swept against bias."""
    code, name, category, component = "ict_2023_model", "ICT 2023 Model", CAT, C_SMC_ICT
    params = {"rr": 2.5}

    def detect(self, ctx):
        bias = ctx.htf_bias()
        a = _asia(ctx).iloc[-1]
        if bias == 0 or np.isnan(a.high) or _in(ctx, "ASIA"):
            return None
        today, i, c = _today(ctx), ctx.n - 1, candle(ctx)
        swept = today.low.min() < a.low if bias > 0 else today.high.max() > a.high
        if not swept:
            return None
        for g in reversed(ctx.fvgs(0.1)):
            if g.inverted_idx is None or i - g.inverted_idx > 10 or g.inverted_idx >= i:
                continue
            if bias > 0 and g.direction == "SELL" and c["l"] <= g.top and c["c"] > g.top:
                return self.make(ctx, "BUY", float(today.low.min()), target=float(a.high) if a.high > c["c"] else None,
                                 reasons=["2023: SSL sweep vs bullish bias, IFVG support"])
            if bias < 0 and g.direction == "BUY" and c["h"] >= g.bottom and c["c"] < g.bottom:
                return self.make(ctx, "SELL", float(today.high.max()), target=float(a.low) if a.low < c["c"] else None,
                                 reasons=["2023: BSL sweep vs bearish bias, IFVG resistance"])
        return None


class PowerOfThree(_Ict):
    """Accumulation (Asia) -> manipulation against bias -> distribution entry on return into range."""
    code, name, category, component = "ict_po3", "ICT Power of Three (PO3)", CAT, C_SMC_ICT

    def detect(self, ctx):
        bias = ctx.htf_bias()
        if bias == 0 or not (_in(ctx, "LONDON") or _in(ctx, "NEW_YORK")):
            return None
        a = _asia(ctx).iloc[-1]
        if np.isnan(a.high):
            return None
        today, c = _today(ctx), candle(ctx)
        mo = float(today.open.iloc[0])
        if bias > 0 and today.low.min() < a.low and c["c"] > a.low and c["c"] > mo and c["bull"]:
            return self.make(ctx, "BUY", float(today.low.min()), reasons=["PO3 manipulation below Asia, distribution up"])
        if bias < 0 and today.high.max() > a.high and c["c"] < a.high and c["c"] < mo and c["bear"]:
            return self.make(ctx, "SELL", float(today.high.max()), reasons=["PO3 manipulation above Asia, distribution down"])
        return None


class OptimalTradeEntry(Strategy):
    """62-79% retracement of the latest impulsive (BOS) leg with rejection."""
    code, name, category, component = "ict_ote", "ICT Optimal Trade Entry (OTE)", CAT, C_SMC_ICT
    params = {"strength": 3, "lo": 0.62, "hi": 0.79, "max_age": 40}

    def detect(self, ctx):
        ev, _ = ctx.events(self.p["strength"])
        i, c = ctx.n - 1, candle(ctx)
        bos = [e for e in ev if e.kind == "BOS" and i - e.idx <= self.p["max_age"]]
        if not bos:
            return None
        e = bos[-1]
        seg = ctx.df.iloc[e.swing_idx - 10 if e.swing_idx >= 10 else 0: i + 1]
        if e.direction == "BUY":
            lo = float(seg.low.min())
            hi = float(ctx.df.high.iloc[e.idx:i + 1].max())
            z_top, z_bot = hi - self.p["lo"] * (hi - lo), hi - self.p["hi"] * (hi - lo)
            if c["l"] <= z_top and c["l"] >= z_bot - 0.1 * (hi - lo) and c["c"] > z_top and c["bull"]:
                return self.make(ctx, "BUY", lo, target=hi + 0.27 * (hi - lo), reasons=["OTE 62-79% of bullish leg"])
        else:
            hi = float(seg.high.max())
            lo = float(ctx.df.low.iloc[e.idx:i + 1].min())
            z_bot, z_top = lo + self.p["lo"] * (hi - lo), lo + self.p["hi"] * (hi - lo)
            if c["h"] >= z_bot and c["h"] <= z_top + 0.1 * (hi - lo) and c["c"] < z_bot and c["bear"]:
                return self.make(ctx, "SELL", hi, target=lo - 0.27 * (hi - lo), reasons=["OTE 62-79% of bearish leg"])
        return None


class KillZones(_Ict):
    """Kill-zone displacement candle in the higher-timeframe bias direction."""
    code, name, category, component = "ict_killzones", "ICT Kill Zones", CAT, C_SESSION
    params = {"mult": 1.5, "rr": 2.0}

    def detect(self, ctx):
        if not any(_in(ctx, k) for k in ("LONDON_KZ", "NY_AM_KZ", "NY_PM_KZ", "LONDON_CLOSE_KZ")):
            return None
        bias = ctx.htf_bias()
        d = st.displacement(ctx.df.iloc[-60:], self.p["mult"]).iloc[-1]
        c = candle(ctx)
        if bias > 0 and d > 0:
            return self.make(ctx, "BUY", c["l"], reasons=["Kill-zone bullish displacement with HTF bias"])
        if bias < 0 and d < 0:
            return self.make(ctx, "SELL", c["h"], reasons=["Kill-zone bearish displacement with HTF bias"])
        return None


class LondonSession(_Ict):
    """London kill-zone close beyond the Asian range in HTF bias direction."""
    code, name, category, component = "ict_london", "ICT London Session", CAT, C_SESSION

    def detect(self, ctx):
        if not _in(ctx, "LONDON_KZ"):
            return None
        a, bias, c = _asia(ctx).iloc[-1], ctx.htf_bias(), candle(ctx)
        if np.isnan(a.high):
            return None
        p = ctx.df.close.iloc[-2]
        if bias >= 0 and p <= a.high < c["c"]:
            return self.make(ctx, "BUY", float((a.high + a.low) / 2), reasons=["London break of Asia high"])
        if bias <= 0 and p >= a.low > c["c"]:
            return self.make(ctx, "SELL", float((a.high + a.low) / 2), reasons=["London break of Asia low"])
        return None


class NewYorkSession(_Ict):
    """NY AM continuation of the London move after a retrace to the London midpoint."""
    code, name, category, component = "ict_new_york", "ICT New York Session", CAT, C_SESSION

    def detect(self, ctx):
        if not _in(ctx, "NY_AM_KZ"):
            return None
        lmask = ss.session_mask(ctx.df.index, "LONDON_KZ").values
        today_mask = (ss.ny_trading_day(ctx.df.index) == ss.ny_trading_day(ctx.df.index)[-1])
        lon = ctx.df[lmask & np.asarray(today_mask)]
        if len(lon) < 3:
            return None
        move = lon.close.iloc[-1] - lon.open.iloc[0]
        mid = (lon.high.max() + lon.low.min()) / 2
        c = candle(ctx)
        if move > 0 and c["l"] <= mid and c["c"] > mid and c["bull"]:
            return self.make(ctx, "BUY", float(lon.low.min()), target=None, reasons=["NY continuation of London rally"])
        if move < 0 and c["h"] >= mid and c["c"] < mid and c["bear"]:
            return self.make(ctx, "SELL", float(lon.high.max()), reasons=["NY continuation of London sell-off"])
        return None


class AsianRange(_Ict):
    """During Asia, fade the edges of an established tight range toward its midpoint."""
    code, name, category, component = "ict_asian_range", "ICT Asian Range", CAT, C_SESSION
    params = {"min_bars": 8, "edge": 0.15, "max_range_atr": 3.0}
    regimes = ["SIDEWAYS", "CONSOLIDATION", "LOW_VOLATILITY"]

    def detect(self, ctx):
        if not _in(ctx, "ASIA"):
            return None
        m = ss.session_mask(ctx.df.index, "ASIA").values
        i = ctx.n - 1
        s = i
        while s > 0 and m[s - 1]:
            s -= 1
        seg = ctx.df.iloc[s:i]  # range established before current bar
        if len(seg) < self.p["min_bars"]:
            return None
        hi, lo = float(seg.high.max()), float(seg.low.min())
        if hi - lo > self.p["max_range_atr"] * ctx.atr_last():
            return None
        c, mid = candle(ctx), (hi + lo) / 2
        if c["l"] <= lo + self.p["edge"] * (hi - lo) and c["c"] > lo and c["bull"]:
            return self.make(ctx, "BUY", lo, target=mid, reasons=["Asian range low fade"])
        if c["h"] >= hi - self.p["edge"] * (hi - lo) and c["c"] < hi and c["bear"]:
            return self.make(ctx, "SELL", hi, target=mid, reasons=["Asian range high fade"])
        return None


class TurtleSoup(Strategy):
    """False break of an N-bar extreme that is at least 4 bars old, then close back inside."""
    code, name, category, component = "ict_turtle_soup", "ICT Turtle Soup", CAT, C_LIQUIDITY
    params = {"lookback": 20, "min_age": 4, "rr": 2.0}

    def detect(self, ctx):
        prev = ctx.df.iloc[-self.p["lookback"] - 1:-1]
        c = candle(ctx)
        hi_age = len(prev) - 1 - int(np.argmax(prev.high.values))
        lo_age = len(prev) - 1 - int(np.argmin(prev.low.values))
        if c["h"] > prev.high.max() and c["c"] < prev.high.max() and hi_age >= self.p["min_age"]:
            return self.make(ctx, "SELL", c["h"], reasons=[f"Turtle soup above {self.p['lookback']}-bar high"])
        if c["l"] < prev.low.min() and c["c"] > prev.low.min() and lo_age >= self.p["min_age"]:
            return self.make(ctx, "BUY", c["l"], reasons=[f"Turtle soup below {self.p['lookback']}-bar low"])
        return None


class MarketMakerModel(Strategy):
    """MMBM/MMSM: consolidation -> engineered leg away -> sweep + CHOCH -> target the origin."""
    code, name, category, component = "ict_market_maker", "ICT Market Maker Model", CAT, C_SMC_ICT
    params = {"leg_bars": 60, "min_leg_atr": 6.0}

    def detect(self, ctx):
        ev, _ = ctx.events(2)
        i = ctx.n - 1
        if not ev or ev[-1].idx != i or ev[-1].kind != "CHOCH":
            return None
        e, a = ev[-1], ctx.atr_last()
        leg = ctx.df.iloc[-self.p["leg_bars"]:]
        if e.direction == "BUY":
            origin, low = float(leg.high.iloc[:10].max()), float(leg.low.min())
            if origin - low >= self.p["min_leg_atr"] * a:
                return self.make(ctx, "BUY", low, target=origin, strength=0.7, reasons=["MMBM smart money reversal"])
        else:
            origin, high = float(leg.low.iloc[:10].min()), float(leg.high.max())
            if high - origin >= self.p["min_leg_atr"] * a:
                return self.make(ctx, "SELL", high, target=origin, strength=0.7, reasons=["MMSM smart money reversal"])
        return None


class LiquidityRaid(_Ict):
    """Kill-zone raid on previous-day high/low with a close back inside."""
    code, name, category, component = "ict_liquidity_raid", "ICT Liquidity Raid", CAT, C_LIQUIDITY

    def detect(self, ctx):
        if not any(_in(ctx, k) for k in ("LONDON_KZ", "NY_AM_KZ", "NY_PM_KZ")):
            return None
        pl, c = _pd_levels(ctx), candle(ctx)
        if np.isnan(pl.get("high", np.nan)):
            return None
        if c["h"] > pl.high and c["c"] < pl.high:
            return self.make(ctx, "SELL", c["h"], target=float(pl.low), reasons=["PDH raid in kill zone"])
        if c["l"] < pl.low and c["c"] > pl.low:
            return self.make(ctx, "BUY", c["l"], target=float(pl.high), reasons=["PDL raid in kill zone"])
        return None


class DailyBias(_Ict):
    """Bias-aligned entry below (bullish) / above (bearish) the NY midnight open in NY AM."""
    code, name, category, component = "ict_daily_bias", "ICT Daily Bias", CAT, C_STRUCTURE

    def detect(self, ctx):
        if not _in(ctx, "NY_AM_KZ"):
            return None
        d = ctx.htf.get("D1")
        bias = ctx.htf_bias("D1") if d is not None else ctx.htf_bias()
        mo = float(ss.ny_midnight_open(ctx.df).iloc[-1])
        c = candle(ctx)
        today = _today(ctx)
        if bias > 0 and c["l"] < mo and c["c"] > mo and c["bull"]:
            return self.make(ctx, "BUY", float(today.low.min()), reasons=["Bullish daily bias, reclaim of midnight open"])
        if bias < 0 and c["h"] > mo and c["c"] < mo and c["bear"]:
            return self.make(ctx, "SELL", float(today.high.max()), reasons=["Bearish daily bias, loss of midnight open"])
        return None


class WeeklyBias(Strategy):
    """Weekly bias from the prior week's candle; buy below / sell above the weekly open early in the week."""
    code, name, category, component = "ict_weekly_bias", "ICT Weekly Bias", CAT, C_STRUCTURE
    timeframes = ["M15", "H1", "H4"]

    def detect(self, ctx):
        lv = ctx.memo(("pw",), lambda: st.period_levels(ctx.df, "W")).iloc[-1]
        if np.isnan(lv.get("close", np.nan)):
            return None
        idx = ss.local_index(ctx.df.index, "America/New_York")
        if idx[-1].dayofweek > 2:  # Mon-Wed only
            return None
        wp = idx.tz_localize(None).to_period("W")
        week = ctx.df[wp == wp[-1]]
        wo = float(week.open.iloc[0])
        bias = 1 if lv.close > (lv.high + lv.low) / 2 else -1
        c = candle(ctx)
        if bias > 0 and c["l"] < wo < c["c"] and c["bull"]:
            return self.make(ctx, "BUY", float(week.low.min()), target=float(lv.high) if lv.high > c["c"] else None,
                             reasons=["Bullish weekly bias, reclaim of weekly open"])
        if bias < 0 and c["h"] > wo > c["c"] and c["bear"]:
            return self.make(ctx, "SELL", float(week.high.max()), target=float(lv.low) if lv.low < c["c"] else None,
                             reasons=["Bearish weekly bias, rejection of weekly open"])
        return None


class SmtDivergence(Strategy):
    """Correlated-pair divergence at a swing: one makes a new extreme, the other fails."""
    code, name, category, component = "ict_smt", "ICT SMT Divergence", CAT, C_SMC_ICT
    requires = [CAP_PEERS]
    params = {"lookback": 20, "inverse": False}

    def detect(self, ctx):
        if not ctx.peers:
            return None
        peer = next(iter(ctx.peers.values())).reindex(ctx.df.index).ffill()
        lb = self.p["lookback"]
        a, b = ctx.df.iloc[-lb - 1:], peer.iloc[-lb - 1:]
        if b.isna().any().any():
            return None
        inv = self.p["inverse"]
        a_ll = a.low.iloc[-1] < a.low.iloc[:-1].min()
        b_ll = (b.high.iloc[-1] > b.high.iloc[:-1].max()) if inv else (b.low.iloc[-1] < b.low.iloc[:-1].min())
        a_hh = a.high.iloc[-1] > a.high.iloc[:-1].max()
        b_hh = (b.low.iloc[-1] < b.low.iloc[:-1].min()) if inv else (b.high.iloc[-1] > b.high.iloc[:-1].max())
        c = candle(ctx)
        if a_ll and not b_ll and c["c"] > c["o"]:
            return self.make(ctx, "BUY", c["l"], strength=0.65, reasons=["Bullish SMT: peer failed to make lower low"])
        if a_hh and not b_hh and c["c"] < c["o"]:
            return self.make(ctx, "SELL", c["h"], strength=0.65, reasons=["Bearish SMT: peer failed to make higher high"])
        return None


class OpeningRange(_Ict):
    """NY 09:30-10:00 opening range: breakout then retest of the broken edge."""
    code, name, category, component = "ict_opening_range", "ICT Opening Range", CAT, C_SESSION

    def detect(self, ctx):
        today = _today(ctx)
        loc = ss.local_index(today.index, "America/New_York")
        mins = loc.hour * 60 + loc.minute
        orng = today[(mins >= 570) & (mins < 600)]
        after = today[mins >= 600]
        if len(orng) == 0 or len(after) < 2 or mins[-1] > 720:
            return None
        hi, lo, c = float(orng.high.max()), float(orng.low.min()), candle(ctx)
        prev = after.iloc[:-1]
        if (prev.close > hi).any() and c["l"] <= hi and c["c"] > hi:
            return self.make(ctx, "BUY", lo + 0.5 * (hi - lo), reasons=["Opening range high retest"])
        if (prev.close < lo).any() and c["h"] >= lo and c["c"] < lo:
            return self.make(ctx, "SELL", hi - 0.5 * (hi - lo), reasons=["Opening range low retest"])
        return None


class ConsequentEncroachment(Strategy):
    """Precise reaction at the 50% (CE) of an open FVG: wick to CE and close back out."""
    code, name, category, component = "ict_ce", "ICT Consequent Encroachment", CAT, C_SMC_ICT
    params = {"tol": 0.15, "max_age": 30}

    def detect(self, ctx):
        i, c = ctx.n - 1, candle(ctx)
        for g in reversed(ctx.fvgs(0.2)):
            if g.idx >= i or i - g.idx > self.p["max_age"] or (g.filled_idx is not None and g.filled_idx < i):
                continue
            tol = self.p["tol"] * (g.top - g.bottom)
            if g.direction == "BUY" and abs(c["l"] - g.mid) <= tol and c["c"] > g.top:
                return self.make(ctx, "BUY", g.bottom, reasons=["CE of bullish FVG held"])
            if g.direction == "SELL" and abs(c["h"] - g.mid) <= tol and c["c"] < g.bottom:
                return self.make(ctx, "SELL", g.top, reasons=["CE of bearish FVG held"])
        return None


class MacroTimes(_Ict):
    """Inside an ICT macro window, a run on the prior 6-bar extreme that reverses toward HTF bias."""
    code, name, category, component = "ict_macro", "ICT Macro Times", CAT, C_SESSION
    timeframes = ["M1", "M5"]

    def detect(self, ctx):
        if not bool(ss.macro_mask(ctx.df.index[-1:]).iloc[-1]):
            return None
        prev, c, bias = ctx.df.iloc[-7:-1], candle(ctx), ctx.htf_bias()
        if bias >= 0 and c["l"] < prev.low.min() and c["c"] > prev.low.min():
            return self.make(ctx, "BUY", c["l"], reasons=["Macro window sell-side run reversed"])
        if bias <= 0 and c["h"] > prev.high.max() and c["c"] < prev.high.max():
            return self.make(ctx, "SELL", c["h"], reasons=["Macro window buy-side run reversed"])
        return None


STRATEGIES = [SilverBullet, Unicorn, JudasSwing, Ict2022, Ict2023, PowerOfThree, OptimalTradeEntry, KillZones,
              LondonSession, NewYorkSession, AsianRange, TurtleSoup, MarketMakerModel, LiquidityRaid, DailyBias,
              WeeklyBias, SmtDivergence, OpeningRange, ConsequentEncroachment, MacroTimes]
_ = last_swing
