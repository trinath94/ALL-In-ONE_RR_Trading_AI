"""Trading sessions, ICT kill zones and macro windows.

Times are defined in the *local* timezone of the market (IANA names), so
daylight-saving transitions in London and New York are handled by zoneinfo rather than
fixed UTC offsets. Bars are expected to carry UTC timestamps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class SessionDef:
    name: str
    tz: str
    start: time
    end: time
    kind: str = "session"


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


DEFAULT_SESSIONS: dict[str, SessionDef] = {
    "ASIA": SessionDef("ASIA", "America/New_York", _t("20:00"), _t("00:00")),  # ICT Asian range (NY time)
    "LONDON": SessionDef("LONDON", "Europe/London", _t("08:00"), _t("16:30")),
    "NEW_YORK": SessionDef("NEW_YORK", "America/New_York", _t("08:00"), _t("17:00")),
    "NY_EQUITIES": SessionDef("NY_EQUITIES", "America/New_York", _t("09:30"), _t("16:00")),
    "LONDON_KZ": SessionDef("LONDON_KZ", "America/New_York", _t("02:00"), _t("05:00"), "killzone"),
    "NY_AM_KZ": SessionDef("NY_AM_KZ", "America/New_York", _t("07:00"), _t("10:00"), "killzone"),
    "NY_PM_KZ": SessionDef("NY_PM_KZ", "America/New_York", _t("13:30"), _t("16:00"), "killzone"),
    "LONDON_CLOSE_KZ": SessionDef("LONDON_CLOSE_KZ", "America/New_York", _t("10:00"), _t("12:00"), "killzone"),
    "SB_LONDON": SessionDef("SB_LONDON", "America/New_York", _t("03:00"), _t("04:00"), "silver_bullet"),
    "SB_NY_AM": SessionDef("SB_NY_AM", "America/New_York", _t("10:00"), _t("11:00"), "silver_bullet"),
    "SB_NY_PM": SessionDef("SB_NY_PM", "America/New_York", _t("14:00"), _t("15:00"), "silver_bullet"),
    "OVERLAP": SessionDef("OVERLAP", "America/New_York", _t("08:00"), _t("11:00")),
}

# ICT macro windows (NY local), each 20 minutes.
MACROS = [("02:33", "03:00"), ("04:03", "04:30"), ("08:50", "09:10"), ("09:50", "10:10"),
          ("10:50", "11:10"), ("11:50", "12:10"), ("13:10", "13:40"), ("15:15", "15:45")]


def local_index(idx: pd.DatetimeIndex, tz: str) -> pd.DatetimeIndex:
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert(ZoneInfo(tz))


def in_window(idx: pd.DatetimeIndex, tz: str, start: time, end: time) -> pd.Series:
    loc = local_index(idx, tz)
    minutes = loc.hour * 60 + loc.minute
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if s < e:
        m = (minutes >= s) & (minutes < e)
    else:  # wraps midnight (e == 0 means until midnight)
        m = (minutes >= s) | (minutes < e)
    return pd.Series(m, index=idx)


def session_mask(idx: pd.DatetimeIndex, name: str, sessions: dict[str, SessionDef] | None = None) -> pd.Series:
    d = (sessions or DEFAULT_SESSIONS)[name]
    return in_window(idx, d.tz, d.start, d.end)


def macro_mask(idx: pd.DatetimeIndex) -> pd.Series:
    m = pd.Series(False, index=idx)
    for a, b in MACROS:
        m |= in_window(idx, "America/New_York", _t(a), _t(b))
    return m


def current_sessions(ts: pd.Timestamp, sessions: dict[str, SessionDef] | None = None) -> list[str]:
    idx = pd.DatetimeIndex([ts])
    return [n for n in (sessions or DEFAULT_SESSIONS) if bool(session_mask(idx, n, sessions).iloc[0])]


def ny_trading_day(idx: pd.DatetimeIndex) -> pd.Index:
    """ICT trading day starts at NY midnight."""
    return local_index(idx, "America/New_York").floor("D").tz_localize(None)


def ny_midnight_open(df: pd.DataFrame) -> pd.Series:
    day = ny_trading_day(df.index)
    return df["open"].groupby(day).transform("first")


def is_weekend_gap(idx: pd.DatetimeIndex) -> pd.Series:
    loc = local_index(idx, "America/New_York")
    return pd.Series((loc.dayofweek == 6) & (loc.hour < 17), index=idx)
