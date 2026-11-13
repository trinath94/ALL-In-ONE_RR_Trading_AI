"""Strategy framework.

Every strategy evaluates the *last closed bar* of `ctx.df` and returns either a
StrategySignal or None. Strategies never see future bars: the backtester and live
scanner both pass frames truncated at the decision bar.
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.analysis import structure as st

# Data capabilities a strategy may require. The scanner populates ctx.data_caps from what
# the broker actually provides. Strategies whose requirements are not met never fire.
CAP_TICK_VOLUME = "tick_volume"
CAP_REAL_VOLUME = "real_volume"       # centralised exchange volume
CAP_ORDER_FLOW = "order_flow"         # bid/ask aggressor trades (footprint, delta, CVD)
CAP_DOM = "market_depth"              # level-2 book (iceberg detection)
CAP_OPEN_INTEREST = "open_interest"
CAP_PEERS = "peer_symbols"            # correlated symbol data (SMT, pairs)
CAP_HFT = "hft_infrastructure"        # co-located low-latency execution
CAP_MODEL = "trained_model"           # validated ML model artefact

# Scoring components (map to the signal-engine weight table)
C_STRUCTURE = "market_structure"
C_SMC_ICT = "smc_ict"
C_PRICE_ACTION = "price_action"
C_TREND = "trend"
C_INDICATOR = "indicator"
C_LIQUIDITY = "liquidity"
C_VOLUME = "volume"
C_MTF = "mtf"
C_SESSION = "session_volatility"


@dataclass
class StrategySignal:
    strategy: str
    direction: str            # BUY | SELL
    entry: float
    stop_loss: float
    take_profit: float
    strength: float = 0.5     # 0..1 quality of the pattern (NOT a probability)
    reasons: list[str] = field(default_factory=list)
    component: str = C_PRICE_ACTION
    meta: dict = field(default_factory=dict)

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry - self.stop_loss)
        return abs(self.take_profit - self.entry) / risk if risk > 0 else 0.0

    def valid(self) -> bool:
        if self.direction == "BUY":
            return self.stop_loss < self.entry < self.take_profit
        return self.take_profit < self.entry < self.stop_loss

    def dict(self) -> dict:
        d = asdict(self)
        d["risk_reward"] = round(self.risk_reward, 3)
        return d


class StrategyContext:
    """Bars up to the decision bar plus memoised indicator access."""

    def __init__(self, df: pd.DataFrame, symbol: str = "", timeframe: str = "H1",
                 htf: dict[str, pd.DataFrame] | None = None, peers: dict[str, pd.DataFrame] | None = None,
                 point: float = 0.00001, data_caps: set[str] | None = None, aux: dict | None = None):
        self.df = df
        self.symbol = symbol
        self.timeframe = timeframe
        self.htf = htf or {}
        self.peers = peers or {}
        self.point = point
        self.data_caps = data_caps if data_caps is not None else {CAP_TICK_VOLUME}
        # Optional auxiliary feeds keyed by capability, e.g. aux["order_flow"] -> DataFrame with
        # buy_volume/sell_volume aligned to df; aux["open_interest"] -> Series; aux["market_depth"] -> list.
        self.aux = aux or {}
        self._cache: dict[tuple, Any] = {}

    def memo(self, key: tuple, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # Convenience accessors -------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.df)

    @property
    def close(self) -> pd.Series:
        return self.df["close"]

    def last(self, col: str = "close", k: int = 1) -> float:
        return float(self.df[col].iloc[-k])

    def ema(self, n: int) -> pd.Series:
        return self.memo(("ema", n), lambda: ind.ema(self.close, n))

    def sma(self, n: int) -> pd.Series:
        return self.memo(("sma", n), lambda: ind.sma(self.close, n))

    def atr(self, n: int = 14) -> pd.Series:
        return self.memo(("atr", n), lambda: ind.atr(self.df, n))

    def atr_last(self, n: int = 14) -> float:
        v = self.atr(n).iloc[-1]
        return float(v) if not np.isnan(v) else float((self.df["high"] - self.df["low"]).iloc[-n:].mean())

    def rsi(self, n: int = 14) -> pd.Series:
        return self.memo(("rsi", n), lambda: ind.rsi(self.close, n))

    def macd(self, f: int = 12, s: int = 26, g: int = 9) -> pd.DataFrame:
        return self.memo(("macd", f, s, g), lambda: ind.macd(self.close, f, s, g))

    def adx(self, n: int = 14) -> pd.DataFrame:
        return self.memo(("adx", n), lambda: ind.adx(self.df, n))

    def bb(self, n: int = 20, k: float = 2.0) -> pd.DataFrame:
        return self.memo(("bb", n, k), lambda: ind.bollinger(self.close, n, k))

    def swings(self, strength: int = 3) -> list[st.Swing]:
        return self.memo(("swings", strength), lambda: st.swings(self.df, strength))

    def known_swings(self, strength: int = 3) -> list[st.Swing]:
        return st.known_swings(self.swings(strength), self.n - 1)

    def events(self, strength: int = 3):
        return self.memo(("events", strength), lambda: st.structure_events(self.df, strength))

    def fvgs(self, min_atr_frac: float = 0.1) -> list[st.FVG]:
        return self.memo(("fvg", min_atr_frac), lambda: st.fair_value_gaps(self.df, min_atr_frac))

    def order_blocks(self, strength: int = 3) -> list[st.Zone]:
        return self.memo(("ob", strength), lambda: st.order_blocks(self.df, strength))

    def htf_bias(self, tf: str | None = None) -> int:
        """+1/-1/0 from the higher-timeframe EMA50 slope + structure (if provided)."""
        frames = [self.htf[tf]] if tf and tf in self.htf else list(self.htf.values())
        if not frames:
            return 0
        h = frames[0]
        if len(h) < 60:
            return 0
        e = ind.ema(h["close"], 50)
        _, trend = st.structure_events(h.iloc[-200:])
        slope = e.iloc[-1] - e.iloc[-5]
        s = (1 if slope > 0 else -1) + trend
        return 1 if s >= 1 else (-1 if s <= -1 else 0)


@dataclass
class StrategyMeta:
    code: str
    name: str
    category: str
    component: str
    description: str
    params: dict
    timeframes: list[str]
    regimes: list[str]        # regimes where the strategy is suited ([] = all)
    requires: list[str]
    risky: bool = False
    default_enabled: bool = True
    version: str = "1.0.0"
    code_hash: str = ""


class Strategy:
    code: str = ""
    name: str = ""
    category: str = ""
    component: str = C_PRICE_ACTION
    description: str = ""
    params: dict = {}
    timeframes: list[str] = ["M15", "H1", "H4"]
    regimes: list[str] = []
    requires: list[str] = []
    risky: bool = False
    default_enabled: bool = True
    min_bars: int = 60
    version: str = "1.0.0"

    def __init__(self, **overrides):
        self.p = {**self.params, **overrides}

    # Subclasses implement this -------------------------------------------------
    def detect(self, ctx: StrategyContext) -> StrategySignal | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def available(self, ctx: StrategyContext) -> tuple[bool, str]:
        missing = [r for r in self.requires if r not in ctx.data_caps]
        if missing:
            return False, f"required data unavailable: {', '.join(missing)}"
        if ctx.n < self.min_bars:
            return False, f"insufficient bars ({ctx.n} < {self.min_bars})"
        return True, ""

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        ok, _ = self.available(ctx)
        if not ok:
            return None
        sig = self.detect(ctx)
        if sig is None or not sig.valid():
            return None
        sig.strategy = self.code
        sig.component = self.component
        return sig

    @classmethod
    def meta(cls) -> StrategyMeta:
        try:
            src = inspect.getsource(cls)
        except (OSError, TypeError):
            src = cls.code
        doc = next((k.__doc__ for k in cls.__mro__ if k.__doc__ and k is not Strategy), "")
        return StrategyMeta(cls.code, cls.name, cls.category, cls.component, cls.description or " ".join(doc.split()),
                            dict(cls.params), list(cls.timeframes), list(cls.regimes), list(cls.requires), cls.risky,
                            cls.default_enabled and not cls.risky, cls.version,
                            hashlib.sha256(src.encode()).hexdigest()[:16])

    # Helpers ---------------------------------------------------------------------
    def make(self, ctx: StrategyContext, direction: str, stop: float | None = None, rr: float | None = None,
             strength: float = 0.5, reasons: list[str] | None = None, entry: float | None = None,
             target: float | None = None, **meta) -> StrategySignal | None:
        """Build a signal with structural stop (or ATR fallback) and R-multiple target."""
        e = entry if entry is not None else ctx.last("close")
        a = ctx.atr_last()
        buf = self.p.get("sl_buffer_atr", 0.1) * a
        if stop is None:
            k = self.p.get("sl_atr", 1.5)
            stop = e - k * a if direction == "BUY" else e + k * a
        else:
            stop = stop - buf if direction == "BUY" else stop + buf
        risk = abs(e - stop)
        if risk <= 0 or risk > self.p.get("max_sl_atr", 6.0) * a:
            return None
        r = rr if rr is not None else self.p.get("rr", 2.0)
        tp = target if target is not None else (e + r * risk if direction == "BUY" else e - r * risk)
        return StrategySignal(self.code, direction, float(e), float(stop), float(tp),
                              float(np.clip(strength, 0, 1)), reasons or [], self.component, meta)


def cross_up(a: pd.Series, b: pd.Series | float) -> bool:
    bb = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return bool(a.iloc[-2] <= bb.iloc[-2] and a.iloc[-1] > bb.iloc[-1])


def cross_down(a: pd.Series, b: pd.Series | float) -> bool:
    bb = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return bool(a.iloc[-2] >= bb.iloc[-2] and a.iloc[-1] < bb.iloc[-1])


def last_swing(ctx: StrategyContext, kind: str, strength: int = 3, before: int | None = None) -> st.Swing | None:
    sw = [s for s in ctx.known_swings(strength) if s.kind == kind and (before is None or s.idx < before)]
    return max(sw, key=lambda s: s.idx) if sw else None


def recent_low(ctx: StrategyContext, n: int = 10) -> float:
    return float(ctx.df["low"].iloc[-n:].min())


def recent_high(ctx: StrategyContext, n: int = 10) -> float:
    return float(ctx.df["high"].iloc[-n:].max())


def candle(ctx: StrategyContext, k: int = 1) -> dict:
    r = ctx.df.iloc[-k]
    o, h, l, c = float(r.open), float(r.high), float(r.low), float(r.close)
    rng = max(h - l, 1e-12)
    return {"o": o, "h": h, "l": l, "c": c, "body": abs(c - o), "range": rng, "bull": c > o, "bear": c < o,
            "upper": h - max(o, c), "lower": min(o, c) - l}
