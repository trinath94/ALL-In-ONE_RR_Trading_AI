"""Key levels, the way a professional trader marks the chart before the session.

From the bars the scanner already loaded (no extra broker call, no AI):
* previous day high, low and close;
* this week's open, previous week high and low;
* today's Asian range high and low (00:00-07:00 UTC, intraday timeframes only);
* the nearest round numbers above and below the price (every 500 points, e.g. 1.0850 / 1.0900 on EURUSD, 2650 / 2655 on
  gold).

``nearest`` gives the closest level and its distance in ATR. The A+ setup gate uses it: a trader takes trades that react
at a level, not in the middle of nowhere.
"""
from __future__ import annotations

import math

import pandas as pd

ROUND_STEP_POINTS = 500
ASIA_END_HOUR = 7


def compute(df: pd.DataFrame, point: float, price: float | None = None) -> list[dict]:
    """Levels from an OHLC frame indexed by UTC bar time. Missing history simply yields fewer levels."""
    if df is None or not len(df):
        return []
    price = float(price if price is not None else df["close"].iloc[-1])
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    frame = df.set_axis(idx)
    out: list[dict] = []
    days = frame.groupby(idx.normalize())
    keys = sorted(days.groups)
    if len(keys) >= 2:
        prev = days.get_group(keys[-2])
        out += [{"name": "previous day high", "price": float(prev["high"].max())},
                {"name": "previous day low", "price": float(prev["low"].min())},
                {"name": "previous day close", "price": float(prev["close"].iloc[-1])}]
    week = idx.to_period("W-SUN")
    weeks = sorted(set(week))
    this_week = frame[week == weeks[-1]]
    if len(this_week):
        out.append({"name": "week open", "price": float(this_week["open"].iloc[0])})
    if len(weeks) >= 2:
        pw = frame[week == weeks[-2]]
        out += [{"name": "previous week high", "price": float(pw["high"].max())},
                {"name": "previous week low", "price": float(pw["low"].min())}]
    if len(idx) >= 2 and (idx[-1] - idx[-2]) < pd.Timedelta(hours=4):
        today = frame[idx.normalize() == keys[-1]]
        asia = today[today.index.hour < ASIA_END_HOUR]
        if len(asia):
            out += [{"name": "Asian range high", "price": float(asia["high"].max())},
                    {"name": "Asian range low", "price": float(asia["low"].min())}]
    step = ROUND_STEP_POINTS * float(point or 0)
    if step > 0:
        below = math.floor(price / step) * step
        out += [{"name": "round number", "price": round(below, 10)},
                {"name": "round number", "price": round(below + step, 10)}]
    return out


def nearest(levels: list[dict], price: float, atr: float | None) -> dict | None:
    """The closest level and its distance in ATR (None when there are no levels or no ATR)."""
    if not levels or not atr or atr <= 0:
        return None
    lv = min(levels, key=lambda x: abs(x["price"] - price))
    return {**lv, "distance_atr": round(abs(lv["price"] - price) / atr, 3)}


def summary(df: pd.DataFrame, point: float, price: float, atr: float | None) -> dict:
    lv = compute(df, point, price)
    return {"levels": lv, "nearest": nearest(lv, price, atr)}
