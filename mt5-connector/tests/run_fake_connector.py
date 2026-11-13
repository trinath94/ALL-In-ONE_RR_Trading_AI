"""LOCAL TEST HARNESS ONLY — serves the real connector app backed by FakeMT5 (simulated
prices) so the Linux stack and dashboard can be exercised without a Windows terminal.
Never deploy this. Production uses run.py with the MetaTrader5 package."""
import os
import sys

import time

import numpy as np
import pandas as pd
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from connector.adapter import MT5Adapter  # noqa: E402
from connector.app import ConnectorConfig, create_app  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402

print("!!! SIMULATED MT5 (FakeMT5) — TEST HARNESS, NOT A BROKER CONNECTION !!!", file=sys.stderr)
fake = FakeMT5(login=int(os.environ.get("RR_MT5_LOGIN", "1001")), server=os.environ.get("RR_MT5_SERVER", "Demo-Server"))
TF_MIN = {fake.TIMEFRAME_M1: 1, fake.TIMEFRAME_M5: 5, fake.TIMEFRAME_M15: 15, fake.TIMEFRAME_M30: 30,
          fake.TIMEFRAME_H1: 60, fake.TIMEFRAME_H4: 240, fake.TIMEFRAME_D1: 1440}
DT = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
               ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
PATH_MIN = 400_000      # ~9 months of M1 history per symbol
FUTURE_MIN = 14 * 1440  # path continues for 14 days after start-up, revealed minute by minute
_paths: dict = {}


def m1_path(name):
    """One consistent simulated M1 price path per symbol; every timeframe and the live tick derive from it."""
    if name not in _paths:
        rng = np.random.default_rng(abs(hash(name)) % 2**32)
        p0 = fake.symbols[name]["bid"]
        n = PATH_MIN + FUTURE_MIN
        regime = np.sign(np.sin(np.arange(n) / 9000.0))
        r = rng.normal(regime * 0.000004, 0.00022, n)
        close = p0 * np.exp(np.cumsum(r))
        end = int(time.time()) // 60 * 60
        _paths[name] = (end - (PATH_MIN - 1) * 60, close, rng.integers(1, 40, n))
    t0, close, vol = _paths[name]
    now_idx = min(len(close) - 1, (int(time.time()) // 60 * 60 - t0) // 60)
    return t0, close[: now_idx + 1], vol[: now_idx + 1]


def rates(name, tf, start, count):
    t0, close, vol = m1_path(name)
    k = TF_MIN.get(tf, 60)
    times = t0 + np.arange(len(close)) * 60
    bucket = times // (k * 60)
    df = pd.DataFrame({"b": bucket, "c": close, "v": vol})
    g = df.groupby("b")
    agg = pd.DataFrame({"open": g.c.first(), "high": g.c.max(), "low": g.c.min(), "close": g.c.last(), "v": g.v.sum()}).iloc[-count:]
    prev = agg["close"].shift(1).fillna(agg["open"])
    agg["open"] = prev
    agg["high"] = np.maximum(agg["high"], agg["open"])
    agg["low"] = np.minimum(agg["low"], agg["open"])
    s = fake.symbols[name]
    s["bid"] = float(close[-1]); s["ask"] = float(close[-1]) + 10 * s["point"]
    out = np.zeros(len(agg), dtype=DT)
    out["time"] = agg.index.to_numpy() * k * 60
    for c in ("open", "high", "low", "close"):
        out[c] = agg[c].to_numpy()
    out["tick_volume"], out["spread"] = agg["v"].to_numpy(), 10
    return out


_orig_tick = fake.symbol_info_tick


def tick(name):
    _, close, _ = m1_path(name)
    s = fake.symbols[name]
    s["bid"] = float(close[-1]); s["ask"] = float(close[-1]) + 10 * s["point"]
    return _orig_tick(name)


fake.symbol_info_tick = tick
fake.copy_rates_from_pos = rates
cfg = ConnectorConfig(os.environ["RR_BRIDGE_SHARED_SECRET"], fake.login, fake.server,
                      journal_path=os.environ.get("RR_JOURNAL_PATH", "/tmp/rr-fake-journal.db"))
uvicorn.run(create_app(cfg, MT5Adapter(fake), connect=False), host=os.environ.get("RR_BIND", "127.0.0.1"), port=int(os.environ.get("RR_PORT", "8443")))
