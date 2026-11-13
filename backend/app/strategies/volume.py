"""Advanced institutional & volume strategies (15).

Data honesty rules
------------------
* OTC FX/CFD brokers only provide *tick volume* (number of price changes). Strategies
  using it label their reasons accordingly.
* Delta / CVD / footprint / order-flow require aggressor-side trade data
  (CAP_ORDER_FLOW). Iceberg detection requires market depth (CAP_DOM). Open-interest
  analysis requires an OI feed (CAP_OPEN_INTEREST). Those strategies contain real
  logic but are reported as *unavailable* and never fire unless that data is
  actually supplied via ctx.aux by a verified provider.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.strategies.base import (C_PRICE_ACTION, C_VOLUME, CAP_DOM, CAP_OPEN_INTEREST, CAP_ORDER_FLOW,
                                 CAP_TICK_VOLUME, Strategy, StrategyContext, candle)

CAT = "volume"


def _vol(ctx: StrategyContext) -> pd.Series:
    return ctx.df["volume"].astype(float)


def _rel_vol(ctx: StrategyContext, n: int = 20) -> float:
    v = _vol(ctx)
    avg = v.iloc[-n - 1:-1].mean()
    return float(v.iloc[-1] / avg) if avg > 0 else 0.0


def _range(ctx: StrategyContext, n: int = 60):
    prev = ctx.df.iloc[-n - 1:-1]
    return float(prev.high.max()), float(prev.low.min())


class VSA(Strategy):
    """Volume Spread Analysis: upthrust (wide up-spread, ultra-high volume, close near low) and
    no-supply (narrow down bar on the lowest volume of 3 bars inside an uptrend)."""
    code, name, category, component = "vol_vsa", "Volume Spread Analysis (VSA)", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        c, a = candle(ctx), ctx.atr_last()
        v = _vol(ctx)
        rv = _rel_vol(ctx)
        hi, _ = _range(ctx, 30)
        pos = (c["c"] - c["l"]) / c["range"]
        if c["range"] > 1.5 * a and rv > 2.0 and pos < 0.3 and c["h"] >= hi:
            return self.make(ctx, "SELL", c["h"], reasons=["VSA upthrust on ultra-high tick volume"])
        up = ctx.ema(20).iloc[-1] > ctx.ema(50).iloc[-1]
        if up and c["bear"] and c["range"] < 0.7 * a and v.iloc[-1] < v.iloc[-3:-1].min():
            return self.make(ctx, "BUY", c["l"], reasons=["VSA no-supply bar in uptrend"])
        return None


def _in_range(ctx, n=60, max_adx=22):
    return ctx.adx().adx.iloc[-2] < max_adx


class WyckoffMethod(Strategy):
    """Sign of strength / weakness: break of a 60-bar trading range on >=1.5x volume."""
    code, name, category, component = "vol_wyckoff", "Wyckoff Method", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        if not _in_range(ctx):
            return None
        hi, lo = _range(ctx)
        c, rv = candle(ctx), _rel_vol(ctx)
        if c["c"] > hi and rv >= 1.5:
            return self.make(ctx, "BUY", (hi + lo) / 2, target=hi + (hi - lo), reasons=["Wyckoff SOS out of range"])
        if c["c"] < lo and rv >= 1.5:
            return self.make(ctx, "SELL", (hi + lo) / 2, target=lo - (hi - lo), reasons=["Wyckoff SOW out of range"])
        return None


class AccumulationDistribution(Strategy):
    """Chaikin A/D line divergence vs price over a 20-bar range."""
    code, name, category, component = "vol_accum_dist", "Accumulation and Distribution", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        df = ctx.df
        clv = ((df.close - df.low) - (df.high - df.close)) / (df.high - df.low).replace(0, np.nan)
        ad = (clv.fillna(0) * df.volume).cumsum()
        p, a = df.close.iloc[-21:], ad.iloc[-21:]
        pr = (p.iloc[-1] - p.min()) / max(p.max() - p.min(), 1e-12)
        ar = (a.iloc[-1] - a.min()) / max(a.max() - a.min(), 1e-12)
        c = candle(ctx)
        if pr < 0.3 and ar > 0.8 and c["bull"]:
            return self.make(ctx, "BUY", float(p.min()), reasons=["Accumulation: A/D strong while price low"])
        if pr > 0.7 and ar < 0.2 and c["bear"]:
            return self.make(ctx, "SELL", float(p.max()), reasons=["Distribution: A/D weak while price high"])
        return None


class WyckoffSpring(Strategy):
    """Break below a trading-range low that closes back inside (spring)."""
    code, name, category, component = "vol_wyckoff_spring", "Wyckoff Spring", CAT, C_VOLUME

    def detect(self, ctx):
        if not _in_range(ctx):
            return None
        hi, lo = _range(ctx)
        c = candle(ctx)
        if c["l"] < lo < c["c"]:
            return self.make(ctx, "BUY", c["l"], target=hi, strength=0.65, reasons=["Wyckoff spring below range"])
        return None


class WyckoffUpthrust(Strategy):
    """Break above a trading-range high that closes back inside (UTAD)."""
    code, name, category, component = "vol_wyckoff_upthrust", "Wyckoff Upthrust", CAT, C_VOLUME

    def detect(self, ctx):
        if not _in_range(ctx):
            return None
        hi, lo = _range(ctx)
        c = candle(ctx)
        if c["h"] > hi > c["c"]:
            return self.make(ctx, "SELL", c["h"], target=lo, strength=0.65, reasons=["Wyckoff upthrust above range"])
        return None


class VolumeClimax(Strategy):
    """Prior bar: >3x volume, wide range at a 50-bar extreme; current bar reverses it."""
    code, name, category, component = "vol_climax", "Volume Climax", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        v = _vol(ctx)
        prev_rv = v.iloc[-2] / max(v.iloc[-22:-2].mean(), 1e-12)
        p, c = candle(ctx, 2), candle(ctx)
        hi, lo = float(ctx.df.high.iloc[-52:-2].max()), float(ctx.df.low.iloc[-52:-2].min())
        if prev_rv < 3 or p["range"] < 1.5 * ctx.atr_last():
            return None
        if p["bear"] and p["l"] <= lo and c["bull"] and c["c"] > (p["o"] + p["c"]) / 2:
            return self.make(ctx, "BUY", p["l"], reasons=["Selling climax reversed"])
        if p["bull"] and p["h"] >= hi and c["bear"] and c["c"] < (p["o"] + p["c"]) / 2:
            return self.make(ctx, "SELL", p["h"], reasons=["Buying climax reversed"])
        return None


class Absorption(Strategy):
    """Effort vs result: >=2x volume on a narrow bar at support/resistance, then confirmation."""
    code, name, category, component = "vol_absorption", "Absorption Trading", CAT, C_VOLUME
    requires = [CAP_TICK_VOLUME]

    def detect(self, ctx):
        v = _vol(ctx)
        p, c = candle(ctx, 2), candle(ctx)
        rv = v.iloc[-2] / max(v.iloc[-22:-2].mean(), 1e-12)
        if rv < 2 or p["range"] > 0.8 * ctx.atr_last():
            return None
        lo, hi = float(ctx.df.low.iloc[-22:-2].min()), float(ctx.df.high.iloc[-22:-2].max())
        if p["l"] <= lo * 1.0005 and c["bull"] and c["c"] > p["h"]:
            return self.make(ctx, "BUY", p["l"], reasons=["Selling absorbed at support"])
        if p["h"] >= hi * 0.9995 and c["bear"] and c["c"] < p["l"]:
            return self.make(ctx, "SELL", p["h"], reasons=["Buying absorbed at resistance"])
        return None


def _flow(ctx) -> pd.DataFrame:
    f = ctx.aux.get(CAP_ORDER_FLOW)
    if f is None:
        raise LookupError("order flow feed missing")
    return f.reindex(ctx.df.index)


class DeltaDivergence(Strategy):
    """Price new extreme while bar delta (buy-sell aggressor volume) diverges."""
    code, name, category, component = "vol_delta_divergence", "Delta Divergence", CAT, C_VOLUME
    requires = [CAP_ORDER_FLOW]

    def detect(self, ctx):
        f = _flow(ctx)
        delta = f.buy_volume - f.sell_volume
        c, hi, lo = candle(ctx), float(ctx.df.high.iloc[-21:-1].max()), float(ctx.df.low.iloc[-21:-1].min())
        if c["h"] > hi and delta.iloc[-1] < 0:
            return self.make(ctx, "SELL", c["h"], reasons=["New high on negative delta"])
        if c["l"] < lo and delta.iloc[-1] > 0:
            return self.make(ctx, "BUY", c["l"], reasons=["New low on positive delta"])
        return None


class CumulativeVolumeDelta(Strategy):
    """CVD trend confirmation: CVD and price break their 20-bar extremes together."""
    code, name, category, component = "vol_cvd", "Cumulative Volume Delta (CVD)", CAT, C_VOLUME
    requires = [CAP_ORDER_FLOW]

    def detect(self, ctx):
        f = _flow(ctx)
        cvd = (f.buy_volume - f.sell_volume).cumsum()
        c = ctx.close
        if c.iloc[-1] > c.iloc[-21:-1].max() and cvd.iloc[-1] > cvd.iloc[-21:-1].max():
            return self.make(ctx, "BUY", None, reasons=["Price and CVD breakout"])
        if c.iloc[-1] < c.iloc[-21:-1].min() and cvd.iloc[-1] < cvd.iloc[-21:-1].min():
            return self.make(ctx, "SELL", None, reasons=["Price and CVD breakdown"])
        return None


class FootprintChart(Strategy):
    """Stacked diagonal imbalances (>=3 levels of 3:1 buy/sell) from footprint data."""
    code, name, category, component = "vol_footprint", "Footprint Chart Trading", CAT, C_VOLUME
    requires = [CAP_ORDER_FLOW]

    def detect(self, ctx):
        fp = ctx.aux.get("footprint")  # list[dict(price, bid_vol, ask_vol)] for the last bar
        if not fp:
            return None
        levels = sorted(fp, key=lambda r: r["price"])
        buy_imb = sum(1 for lo, hi in zip(levels, levels[1:]) if hi["ask_vol"] >= 3 * max(lo["bid_vol"], 1))
        sell_imb = sum(1 for lo, hi in zip(levels, levels[1:]) if lo["bid_vol"] >= 3 * max(hi["ask_vol"], 1))
        if buy_imb >= 3:
            return self.make(ctx, "BUY", None, reasons=[f"{buy_imb} stacked buy imbalances"])
        if sell_imb >= 3:
            return self.make(ctx, "SELL", None, reasons=[f"{sell_imb} stacked sell imbalances"])
        return None


class OrderFlow(Strategy):
    """Aggressor ratio > 65% sustained for 3 bars in the trend direction."""
    code, name, category, component = "vol_order_flow", "Order Flow Trading", CAT, C_VOLUME
    requires = [CAP_ORDER_FLOW]

    def detect(self, ctx):
        f = _flow(ctx).iloc[-3:]
        ratio = f.buy_volume / (f.buy_volume + f.sell_volume).replace(0, np.nan)
        if (ratio > 0.65).all():
            return self.make(ctx, "BUY", None, reasons=["Sustained buy aggression"])
        if (ratio < 0.35).all():
            return self.make(ctx, "SELL", None, reasons=["Sustained sell aggression"])
        return None


class MarketAuction(Strategy):
    """Auction theory: two consecutive closes outside the prior 100-bar value area = acceptance."""
    code, name, category, component = "vol_auction", "Market Auction Theory", CAT, C_PRICE_ACTION

    def detect(self, ctx):
        prior = ctx.df.iloc[-102:-2].assign(volume=1)  # time-at-price
        vp = ind.volume_profile(prior, 24)
        if vp["poc"] is None:
            return None
        c1, c2 = ctx.df.close.iloc[-2], ctx.df.close.iloc[-1]
        c0 = ctx.df.close.iloc[-3]
        if c0 <= vp["vah"] < c1 and c2 > vp["vah"]:
            return self.make(ctx, "BUY", vp["poc"], reasons=["Acceptance above value (imbalance up)"])
        if c0 >= vp["val"] > c1 and c2 < vp["val"]:
            return self.make(ctx, "SELL", vp["poc"], reasons=["Acceptance below value (imbalance down)"])
        return None


class IcebergOrders(Strategy):
    """Repeated refills of displayed size at one price in the DOM (>=4 refills)."""
    code, name, category, component = "vol_iceberg", "Iceberg Order Analysis", CAT, C_VOLUME
    requires = [CAP_DOM]

    def detect(self, ctx):
        refills = ctx.aux.get("iceberg_refills")  # {"price": float, "side": "bid"|"ask", "count": int}
        if not refills or refills.get("count", 0) < 4:
            return None
        if refills["side"] == "bid":
            return self.make(ctx, "BUY", refills["price"], reasons=["Bid iceberg detected"])
        return self.make(ctx, "SELL", refills["price"], reasons=["Ask iceberg detected"])


class VolumeImbalance(Strategy):
    """ICT volume imbalance: gap between consecutive candle bodies; first retest respected."""
    code, name, category, component = "vol_volume_imbalance", "Volume Imbalance", CAT, C_PRICE_ACTION
    params = {"max_age": 20}

    def detect(self, ctx):
        o, cl = ctx.df.open.to_numpy(float), ctx.df.close.to_numpy(float)
        i, c = ctx.n - 1, candle(ctx)
        for k in range(i - 1, max(i - self.p["max_age"], 1), -1):
            if cl[k - 1] > o[k - 1] and cl[k] > o[k] and o[k] > cl[k - 1]:  # bullish body gap
                top, bot = o[k], cl[k - 1]
                if ctx.df.low.iloc[k + 1:i].min() > top if k + 1 < i else True:
                    if c["l"] <= top and c["c"] > bot and c["bull"]:
                        return self.make(ctx, "BUY", bot, reasons=["Bullish volume imbalance retest"])
            if cl[k - 1] < o[k - 1] and cl[k] < o[k] and o[k] < cl[k - 1]:
                top, bot = cl[k - 1], o[k]
                if ctx.df.high.iloc[k + 1:i].max() < bot if k + 1 < i else True:
                    if c["h"] >= bot and c["c"] < top and c["bear"]:
                        return self.make(ctx, "SELL", top, reasons=["Bearish volume imbalance retest"])
        return None


class OpenInterest(Strategy):
    """Rising price + rising OI = new longs; falling price + rising OI = new shorts."""
    code, name, category, component = "vol_open_interest", "Open Interest Analysis", CAT, C_VOLUME
    requires = [CAP_OPEN_INTEREST]

    def detect(self, ctx):
        oi = ctx.aux.get(CAP_OPEN_INTEREST)
        if oi is None:
            return None
        oi = oi.reindex(ctx.df.index).ffill()
        dp = ctx.close.iloc[-1] - ctx.close.iloc[-6]
        doi = oi.iloc[-1] - oi.iloc[-6]
        if dp > 0 and doi > 0:
            return self.make(ctx, "BUY", None, reasons=["Price up with rising OI"])
        if dp < 0 and doi > 0:
            return self.make(ctx, "SELL", None, reasons=["Price down with rising OI"])
        return None


STRATEGIES = [VSA, WyckoffMethod, AccumulationDistribution, WyckoffSpring, WyckoffUpthrust, VolumeClimax, Absorption,
              DeltaDivergence, CumulativeVolumeDelta, FootprintChart, OrderFlow, MarketAuction, IcebergOrders,
              VolumeImbalance, OpenInterest]
