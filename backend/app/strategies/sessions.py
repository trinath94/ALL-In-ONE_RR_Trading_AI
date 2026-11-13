"""Time-based and session strategies (15). All times are evaluated in the market's own
local timezone (zoneinfo) so DST shifts are automatic."""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from app.analysis import sessions as ss
from app.analysis import structure as st
from app.strategies.base import C_SESSION, Strategy, StrategyContext, candle

CAT = "sessions"
CAP_NEWS = "news_feed"


def _local_minutes(ctx: StrategyContext, tz: str) -> np.ndarray:
    loc = ss.local_index(ctx.df.index, tz)
    return np.asarray(loc.hour * 60 + loc.minute)


def _same_local_day(ctx: StrategyContext, tz: str) -> np.ndarray:
    loc = ss.local_index(ctx.df.index, tz).floor("D")
    return np.asarray(loc == loc[-1])


class _Intraday(Strategy):
    category, component = CAT, C_SESSION
    timeframes = ["M5", "M15"]
    min_bars = 150

    def available(self, ctx):
        if ctx.timeframe not in ("M1", "M5", "M15", "M30"):
            return False, "intraday timeframe required"
        return super().available(ctx)


class _WindowBreakout(_Intraday):
    """Break of a range built in [r_start, r_end) local time, valid until trade_end."""
    tz, r_start, r_end, trade_end = "America/New_York", 570, 600, 720

    def detect(self, ctx):
        mins, today = _local_minutes(ctx, self.tz), _same_local_day(ctx, self.tz)
        if not (self.r_end <= mins[-1] < self.trade_end):
            return None
        rng_mask = today & (mins >= self.r_start) & (mins < self.r_end)
        if rng_mask.sum() < 1:
            return None
        rng = ctx.df[rng_mask]
        hi, lo = float(rng.high.max()), float(rng.low.min())
        after = ctx.df[today & (mins >= self.r_end)]
        prev_closes = after.close.iloc[:-1]
        c = ctx.last()
        if c > hi and not (prev_closes > hi).any():
            return self.make(ctx, "BUY", (hi + lo) / 2, target=c + (hi - lo), reasons=[f"{self.name}: range high break"])
        if c < lo and not (prev_closes < lo).any():
            return self.make(ctx, "SELL", (hi + lo) / 2, target=c - (hi - lo), reasons=[f"{self.name}: range low break"])
        return None


class AsianBreakout(_Intraday):
    """First close beyond the completed Asian range during London/NY."""
    code, name = "sess_asian_breakout", "Asian Session Breakout"

    def detect(self, ctx):
        if not (ss.session_mask(ctx.df.index[-1:], "LONDON").iloc[-1] or ss.session_mask(ctx.df.index[-1:], "NEW_YORK").iloc[-1]):
            return None
        mask = ss.session_mask(ctx.df.index, "ASIA")
        rng = st.session_range(ctx.df, mask)
        hi, lo = rng.high.iloc[-1], rng.low.iloc[-1]
        if np.isnan(hi):
            return None
        day = np.asarray(ss.ny_trading_day(ctx.df.index) == ss.ny_trading_day(ctx.df.index)[-1])
        post = ctx.df[day & ~mask.values].close.iloc[:-1]
        c = ctx.last()
        if c > hi and not (post > hi).any():
            return self.make(ctx, "BUY", (hi + lo) / 2, reasons=["First break above Asian range"])
        if c < lo and not (post < lo).any():
            return self.make(ctx, "SELL", (hi + lo) / 2, reasons=["First break below Asian range"])
        return None


class LondonBreakout(_WindowBreakout):
    """Pre-London (00:00-07:00 London) range broken in London's first 3 hours."""
    code, name, tz, r_start, r_end, trade_end = "sess_london_breakout", "London Breakout", "Europe/London", 0, 420, 600


class NewYorkBreakout(_WindowBreakout):
    """London morning range (03:00-08:00 NY) broken during 08:00-10:00 NY."""
    code, name, tz, r_start, r_end, trade_end = "sess_ny_breakout", "New York Breakout", "America/New_York", 180, 480, 600


class LondonNyOverlap(_Intraday):
    """During the overlap, a close at a new high/low of the day with above-average range."""
    code, name = "sess_overlap", "London-NY Overlap"

    def detect(self, ctx):
        if not ss.session_mask(ctx.df.index[-1:], "OVERLAP").iloc[-1]:
            return None
        day = np.asarray(ss.ny_trading_day(ctx.df.index) == ss.ny_trading_day(ctx.df.index)[-1])
        today = ctx.df[day]
        if len(today) < 5:
            return None
        c = candle(ctx)
        if c["range"] < ctx.atr_last():
            return None
        if c["c"] > today.high.iloc[:-1].max():
            return self.make(ctx, "BUY", c["l"], reasons=["Overlap new high of day"])
        if c["c"] < today.low.iloc[:-1].min():
            return self.make(ctx, "SELL", c["h"], reasons=["Overlap new low of day"])
        return None


class OpeningRangeBreakout(_WindowBreakout):
    """ORB of the London cash open (first 60 minutes)."""
    code, name, tz, r_start, r_end, trade_end = "sess_orb", "Opening Range Breakout (ORB)", "Europe/London", 480, 540, 720


class InitialBalance(_WindowBreakout):
    """NY equities initial balance (09:30-10:30) breakout; target 1x IB extension."""
    code, name, tz, r_start, r_end, trade_end = "sess_initial_balance", "Initial Balance Strategy", "America/New_York", 570, 630, 900


class First15(_WindowBreakout):
    """Break of the first 15 minutes of the NY equities session."""
    code, name, tz, r_start, r_end, trade_end = "sess_first15", "First 15-Minute Breakout", "America/New_York", 570, 585, 690
    timeframes = ["M1", "M5"]


class First30(_WindowBreakout):
    """Break of the first 30 minutes of the NY equities session."""
    code, name, tz, r_start, r_end, trade_end = "sess_first30", "First 30-Minute Breakout", "America/New_York", 570, 600, 720


class DailyOpen(Strategy):
    """Reclaim / loss of the broker-day open in the EMA50 direction."""
    code, name, category, component = "sess_daily_open", "Daily Open Strategy", CAT, C_SESSION
    timeframes = ["M15", "H1"]

    def detect(self, ctx):
        do = ctx.df.open.groupby(ctx.df.index.floor("D")).transform("first")
        e = ctx.ema(50)
        up = e.iloc[-1] > e.iloc[-5]
        pc, c = ctx.df.close.iloc[-2], ctx.last()
        if up and pc <= do.iloc[-1] < c:
            return self.make(ctx, "BUY", None, reasons=["Reclaimed daily open in uptrend"])
        if not up and pc >= do.iloc[-1] > c:
            return self.make(ctx, "SELL", None, reasons=["Lost daily open in downtrend"])
        return None


class WeeklyOpen(Strategy):
    """Reclaim / loss of the weekly open in the EMA100 direction."""
    code, name, category, component = "sess_weekly_open", "Weekly Open Strategy", CAT, C_SESSION
    timeframes = ["H1", "H4"]
    min_bars = 120

    def detect(self, ctx):
        wk = ss.local_index(ctx.df.index, "America/New_York").tz_localize(None).to_period("W")
        wo = ctx.df.open.groupby(np.asarray(wk)).transform("first")
        e = ctx.ema(100)
        up = e.iloc[-1] > e.iloc[-10]
        pc, c = ctx.df.close.iloc[-2], ctx.last()
        if up and pc <= wo.iloc[-1] < c:
            return self.make(ctx, "BUY", None, reasons=["Reclaimed weekly open"])
        if not up and pc >= wo.iloc[-1] > c:
            return self.make(ctx, "SELL", None, reasons=["Lost weekly open"])
        return None


class MidnightOpen(_Intraday):
    """NY AM: price stretched > 1 ATR from the NY midnight open reverts toward it."""
    code, name = "sess_midnight_open", "Midnight Open Strategy"

    def detect(self, ctx):
        if not ss.session_mask(ctx.df.index[-1:], "NY_AM_KZ").iloc[-1]:
            return None
        mo = float(ss.ny_midnight_open(ctx.df).iloc[-1])
        c, a = candle(ctx), ctx.atr_last()
        if c["c"] > mo + 1.5 * a and c["bear"] and c["upper"] > c["body"]:
            return self.make(ctx, "SELL", c["h"], target=mo, reasons=["Reversion toward midnight open"])
        if c["c"] < mo - 1.5 * a and c["bull"] and c["lower"] > c["body"]:
            return self.make(ctx, "BUY", c["l"], target=mo, reasons=["Reversion toward midnight open"])
        return None


class TimeOfDayReversal(_Intraday):
    """10:00-11:00 NY reversal of a strong opening drive (09:30-10:00)."""
    code, name = "sess_tod_reversal", "Time-of-Day Reversal"

    def detect(self, ctx):
        mins, today = _local_minutes(ctx, "America/New_York"), _same_local_day(ctx, "America/New_York")
        if not (600 <= mins[-1] < 660):
            return None
        drive = ctx.df[today & (mins >= 570) & (mins < 600)]
        if len(drive) == 0:
            return None
        move = drive.close.iloc[-1] - drive.open.iloc[0]
        c, a = candle(ctx), ctx.atr_last()
        if move > 2 * a and c["bear"] and c["c"] < candle(ctx, 2)["l"]:
            return self.make(ctx, "SELL", float(ctx.df[today].high.max()), reasons=["Opening drive up reversed"])
        if move < -2 * a and c["bull"] and c["c"] > candle(ctx, 2)["h"]:
            return self.make(ctx, "BUY", float(ctx.df[today].low.min()), reasons=["Opening drive down reversed"])
        return None


class Seasonal(Strategy):
    """Calendar-month seasonality measured from >= 3 prior years of the symbol's own history."""
    code, name, category, component = "sess_seasonal", "Seasonal Trading", CAT, C_SESSION
    timeframes = ["D1"]
    min_bars = 800
    params = {"min_years": 3, "min_hit": 0.7, "sl_atr": 2.5}

    def detect(self, ctx):
        idx = ctx.df.index
        if idx[-1].month == idx[-2].month:  # trade only on first bar of a month
            return None
        m = ctx.df.close.resample("ME").last().pct_change().dropna()
        m = m[m.index < idx[-1].normalize()]
        same = m[m.index.month == idx[-1].month]
        if len(same) < self.p["min_years"]:
            return None
        hit = (same > 0).mean()
        if hit >= self.p["min_hit"]:
            return self.make(ctx, "BUY", None, reasons=[f"Month {idx[-1].month} up in {hit:.0%} of {len(same)} yrs"])
        if hit <= 1 - self.p["min_hit"]:
            return self.make(ctx, "SELL", None, reasons=[f"Month {idx[-1].month} down in {1-hit:.0%} of {len(same)} yrs"])
        return None


class DayOfWeek(Strategy):
    """Weekday mean-return effect with |t| > 2 measured on the prior >= 52 weeks."""
    code, name, category, component = "sess_day_of_week", "Day-of-Week Trading", CAT, C_SESSION
    timeframes = ["D1"]
    min_bars = 260

    def detect(self, ctx):
        r = ctx.close.pct_change().iloc[:-1].dropna()
        nxt = ctx.df.index[-1] + pd.Timedelta(days=1)
        while nxt.dayofweek >= 5:
            nxt += pd.Timedelta(days=1)
        same = r[r.index.dayofweek == nxt.dayofweek].iloc[-104:]
        if len(same) < 52 or same.std() == 0:
            return None
        t = same.mean() / (same.std() / np.sqrt(len(same)))
        if t > 2:
            return self.make(ctx, "BUY", None, rr=1.0, reasons=[f"Weekday {nxt.day_name()} t={t:.1f}"])
        if t < -2:
            return self.make(ctx, "SELL", None, rr=1.0, reasons=[f"Weekday {nxt.day_name()} t={t:.1f}"])
        return None


class NewsSession(_Intraday):
    """Post high-impact release: trade the close beyond the 30-min pre-news range.
    Requires a connected economic-calendar feed; never fires without one."""
    code, name = "sess_news", "News Session Trading"
    requires = [CAP_NEWS]

    def detect(self, ctx):
        ev = ctx.aux.get(CAP_NEWS)  # {"time": Timestamp(UTC), "impact": "HIGH"}
        if not ev or ev.get("impact") != "HIGH":
            return None
        t = pd.Timestamp(ev["time"])
        now = ctx.df.index[-1]
        if not (t <= now <= t + pd.Timedelta(minutes=45)):
            return None
        pre = ctx.df[(ctx.df.index >= t - pd.Timedelta(minutes=30)) & (ctx.df.index < t)]
        if pre.empty:
            return None
        hi, lo, c = float(pre.high.max()), float(pre.low.min()), ctx.last()
        if c > hi:
            return self.make(ctx, "BUY", lo, reasons=["Post-news break of pre-release range"])
        if c < lo:
            return self.make(ctx, "SELL", hi, reasons=["Post-news break of pre-release range"])
        return None


STRATEGIES = [AsianBreakout, LondonBreakout, NewYorkBreakout, LondonNyOverlap, OpeningRangeBreakout, InitialBalance,
              First15, First30, DailyOpen, WeeklyOpen, MidnightOpen, TimeOfDayReversal, Seasonal, DayOfWeek, NewsSession]
_ = time
