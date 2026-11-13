"""Quantitative & algorithmic strategies (20).

* Grid, Martingale, Anti-Martingale and DCA are flagged ``risky`` -> disabled by default.
  Their size multipliers are *requests* only; the Risk Engine still sizes and caps every
  order and can reject them.
* HFT and market making require co-located, low-latency infrastructure and a level-2
  book. MT5 retail connectivity does not provide this, so they are reported unavailable.
* Volatility arbitrage needs an options implied-volatility feed (not available via MT5).
* Portfolio rebalancing, factor investing and risk parity are *allocation* models; they
  produce target weights (``allocate``) consumed by the portfolio module, not entry signals.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.strategies.base import (C_INDICATOR, C_TREND, CAP_DOM, CAP_HFT, CAP_MODEL, CAP_PEERS, Strategy,
                                 StrategyContext, candle)

CAT = "quantitative"
CAP_IV = "options_implied_vol"
CAP_PORTFOLIO = "portfolio_allocator"
CAP_AGENT = "claude_agent"


def _peer(ctx: StrategyContext) -> pd.Series | None:
    if not ctx.peers:
        return None
    p = next(iter(ctx.peers.values()))["close"].reindex(ctx.df.index).ffill()
    return None if p.isna().iloc[-100:].any() else p


def ols_beta(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    X = np.c_[np.ones_like(x), x]
    a, b = np.linalg.lstsq(X, y, rcond=None)[0]
    return float(a), float(b)


def adf_tstat(series: np.ndarray, lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic on the lagged level coefficient (constant, no trend)."""
    y = np.asarray(series, float)
    dy = np.diff(y)
    rows = []
    for t in range(lags, len(dy)):
        rows.append([1.0, y[t]] + [dy[t - k] for k in range(1, lags + 1)])
    X = np.array(rows)
    Y = dy[lags:]
    coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
    resid = Y - X @ coef
    s2 = resid @ resid / (len(Y) - X.shape[1])
    cov = s2 * np.linalg.inv(X.T @ X)
    return float(coef[1] / np.sqrt(cov[1, 1]))


class StatArb(Strategy):
    """Residual z-score of this symbol regressed on a peer basket (rolling 200 bars)."""
    code, name, category, component = "quant_stat_arb", "Statistical Arbitrage", CAT, C_INDICATOR
    requires = [CAP_PEERS]
    params = {"window": 200, "z": 2.0, "sl_atr": 2.0}
    min_bars = 220

    def detect(self, ctx):
        if not ctx.peers:
            return None
        w = self.p["window"]
        y = np.log(ctx.close.iloc[-w:].values)
        cols = [np.log(p["close"].reindex(ctx.df.index).ffill().iloc[-w:].values) for p in ctx.peers.values()]
        X = np.c_[np.ones(w), np.column_stack(cols)]
        if np.isnan(X).any():
            return None
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        z = (resid[-1] - resid.mean()) / resid.std()
        if z < -self.p["z"]:
            return self.make(ctx, "BUY", None, reasons=[f"Basket residual z={z:.2f}"])
        if z > self.p["z"]:
            return self.make(ctx, "SELL", None, reasons=[f"Basket residual z={z:.2f}"])
        return None


class PairsTrading(Strategy):
    """Rolling-hedge-ratio spread with one peer; enter at |z| > 2, exit at mean (TP)."""
    code, name, category, component = "quant_pairs", "Pairs Trading", CAT, C_INDICATOR
    requires = [CAP_PEERS]
    params = {"window": 120, "z": 2.0}
    min_bars = 150

    def detect(self, ctx):
        p = _peer(ctx)
        if p is None:
            return None
        w = self.p["window"]
        y, x = np.log(ctx.close.iloc[-w:].values), np.log(p.iloc[-w:].values)
        a, b = ols_beta(y, x)
        spread = y - (a + b * x)
        z = (spread[-1] - spread.mean()) / spread.std()
        target = float(np.exp(a + b * x[-1] + spread.mean()))
        if z < -self.p["z"]:
            return self.make(ctx, "BUY", None, target=target, reasons=[f"Pair spread z={z:.2f}, beta={b:.2f}"])
        if z > self.p["z"]:
            return self.make(ctx, "SELL", None, target=target, reasons=[f"Pair spread z={z:.2f}, beta={b:.2f}"])
        return None


class MeanReversionAlgo(Strategy):
    """Ornstein-Uhlenbeck fit: trade |z| > 2 only when estimated half-life is 2-50 bars."""
    code, name, category, component = "quant_mean_reversion", "Mean Reversion Algorithm", CAT, C_INDICATOR
    params = {"window": 150, "z": 2.0, "hl_min": 2, "hl_max": 50}
    regimes = ["SIDEWAYS", "CONSOLIDATION"]
    min_bars = 160

    def detect(self, ctx):
        s = np.log(ctx.close.iloc[-self.p["window"]:].values)
        a, b = ols_beta(np.diff(s), s[:-1])
        if b >= 0:
            return None
        hl = -np.log(2) / b
        if not (self.p["hl_min"] <= hl <= self.p["hl_max"]):
            return None
        mu = -a / b
        z = (s[-1] - mu) / np.std(s)
        tgt = float(np.exp(mu))
        if z < -self.p["z"]:
            return self.make(ctx, "BUY", None, target=tgt, reasons=[f"OU z={z:.2f}, half-life {hl:.1f}"])
        if z > self.p["z"]:
            return self.make(ctx, "SELL", None, target=tgt, reasons=[f"OU z={z:.2f}, half-life {hl:.1f}"])
        return None


class MomentumAlgo(Strategy):
    """Time-series momentum: lookback return / realised vol crosses +/-1."""
    code, name, category, component = "quant_momentum", "Momentum Algorithm", CAT, C_TREND
    params = {"lookback": 50, "threshold": 1.0, "sl_atr": 2.0}
    regimes = ["STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR"]

    def detect(self, ctx):
        r = np.log(ctx.close).diff()
        lb = self.p["lookback"]
        score = (np.log(ctx.close).diff(lb)) / (r.rolling(lb).std() * np.sqrt(lb))
        t = self.p["threshold"]
        if score.iloc[-2] <= t < score.iloc[-1]:
            return self.make(ctx, "BUY", None, reasons=[f"TS momentum score {score.iloc[-1]:.2f}"])
        if score.iloc[-2] >= -t > score.iloc[-1]:
            return self.make(ctx, "SELL", None, reasons=[f"TS momentum score {score.iloc[-1]:.2f}"])
        return None


class TrendFollowingAlgo(Strategy):
    """Composite trend score (EMA20>50>100, ADX>20, close at 20-bar extreme) turning on."""
    code, name, category, component = "quant_trend_following", "Trend-Following Algorithm", CAT, C_TREND
    params = {"sl_atr": 2.5, "rr": 3.0}
    min_bars = 120

    def _score(self, ctx, k):
        e20, e50, e100 = ctx.ema(20).iloc[-k], ctx.ema(50).iloc[-k], ctx.ema(100).iloc[-k]
        adx = ctx.adx().adx.iloc[-k]
        c = ctx.close.iloc[-k]
        hi = ctx.df.high.iloc[-k - 20:-k].max()
        lo = ctx.df.low.iloc[-k - 20:-k].min()
        bull = (e20 > e50 > e100) + (adx > 20) + (c > hi)
        bear = (e20 < e50 < e100) + (adx > 20) + (c < lo)
        return int(bull), int(bear)

    def detect(self, ctx):
        b1, s1 = self._score(ctx, 1)
        b2, s2 = self._score(ctx, 2)
        if b1 == 3 and b2 < 3:
            return self.make(ctx, "BUY", None, reasons=["Trend composite 3/3 bullish"])
        if s1 == 3 and s2 < 3:
            return self.make(ctx, "SELL", None, reasons=["Trend composite 3/3 bearish"])
        return None


class GridTrading(Strategy):
    """Grid of levels every k*ATR inside a range; buy on downward cross, sell on upward cross.
    RISKY: disabled by default and always subject to Risk Engine exposure limits."""
    code, name, category, component = "quant_grid", "Grid Trading", CAT, C_INDICATOR
    risky = True
    params = {"step_atr": 1.0, "sl_atr": 3.0, "rr": 1.0}
    regimes = ["SIDEWAYS", "CONSOLIDATION"]

    def detect(self, ctx):
        a = ctx.atr_last()
        step = self.p["step_atr"] * a
        anchor = float(ctx.sma(100).iloc[-1]) if not np.isnan(ctx.sma(100).iloc[-1]) else ctx.last()
        g_prev = np.floor((ctx.df.close.iloc[-2] - anchor) / step)
        g_now = np.floor((ctx.last() - anchor) / step)
        if g_now < g_prev and g_now < 0:
            return self.make(ctx, "BUY", None, reasons=[f"Grid level {int(g_now)} crossed down"], grid_level=int(g_now))
        if g_now > g_prev and g_now > 0:
            return self.make(ctx, "SELL", None, reasons=[f"Grid level {int(g_now)} crossed up"], grid_level=int(g_now))
        return None


class Martingale(Strategy):
    """Mean-reversion entry requesting size x2^losses after consecutive losses.
    RISKY: disabled by default; the Risk Engine caps the multiplier and total exposure."""
    code, name, category, component = "quant_martingale", "Martingale", CAT, C_INDICATOR
    risky = True
    params = {"max_steps": 4}

    def detect(self, ctx):
        z = ind.zscore(ctx.close, 20).iloc[-1]
        losses = int(ctx.aux.get("consecutive_losses", 0))
        mult = 2 ** min(losses, self.p["max_steps"])
        if z < -2:
            return self.make(ctx, "BUY", None, size_multiplier=mult, reasons=[f"Martingale step {losses} (x{mult})"])
        if z > 2:
            return self.make(ctx, "SELL", None, size_multiplier=mult, reasons=[f"Martingale step {losses} (x{mult})"])
        return None


class AntiMartingale(Strategy):
    """Breakout entry requesting larger size after consecutive wins (pyramiding strength).
    RISKY: disabled by default; capped by the Risk Engine."""
    code, name, category, component = "quant_anti_martingale", "Anti-Martingale", CAT, C_TREND
    risky = True
    params = {"max_steps": 3}

    def detect(self, ctx):
        wins = int(ctx.aux.get("consecutive_wins", 0))
        mult = 1 + 0.5 * min(wins, self.p["max_steps"])
        hi, lo = ctx.df.high.iloc[-21:-1].max(), ctx.df.low.iloc[-21:-1].min()
        if ctx.last() > hi:
            return self.make(ctx, "BUY", None, size_multiplier=mult, reasons=[f"Anti-martingale x{mult} after {wins} wins"])
        if ctx.last() < lo:
            return self.make(ctx, "SELL", None, size_multiplier=mult, reasons=[f"Anti-martingale x{mult} after {wins} wins"])
        return None


class DCA(Strategy):
    """Periodic long accumulation every N bars while price is below its 200-period mean.
    RISKY: disabled by default; still requires a stop and passes the Risk Engine."""
    code, name, category, component = "quant_dca", "DCA (Dollar-Cost Averaging)", CAT, C_INDICATOR
    risky = True
    timeframes = ["D1"]
    params = {"every": 5, "sl_atr": 4.0, "rr": 2.0}
    min_bars = 210

    def detect(self, ctx):
        if ctx.n % self.p["every"] != 0:
            return None
        if ctx.last() < ctx.sma(200).iloc[-1]:
            return self.make(ctx, "BUY", None, reasons=["Scheduled DCA tranche below SMA200"])
        return None


class _PortfolioLevel(Strategy):
    """Allocation model — produces target weights via allocate(); never emits entry signals."""
    category, component = CAT, C_INDICATOR
    requires = [CAP_PORTFOLIO]

    def detect(self, ctx):
        return None

    @staticmethod
    def allocate(returns: pd.DataFrame) -> dict[str, float]:  # pragma: no cover - overridden
        raise NotImplementedError


class PortfolioRebalancing(_PortfolioLevel):
    """Rebalance to target weights when drift exceeds a threshold (equal-weight default)."""
    code, name = "quant_rebalancing", "Portfolio Rebalancing"

    @staticmethod
    def allocate(returns: pd.DataFrame, current: dict[str, float] | None = None, threshold: float = 0.05) -> dict[str, float]:
        target = {c: 1 / returns.shape[1] for c in returns.columns}
        if current and all(abs(current.get(k, 0) - v) < threshold for k, v in target.items()):
            return current
        return target


class FactorInvesting(_PortfolioLevel):
    """Cross-sectional momentum (12-period) minus volatility factor; long top tercile."""
    code, name = "quant_factor", "Factor Investing"

    @staticmethod
    def allocate(returns: pd.DataFrame) -> dict[str, float]:
        mom = returns.iloc[-60:].sum()
        vol = returns.iloc[-60:].std()
        score = (mom.rank() - vol.rank())
        top = score[score >= score.quantile(2 / 3)].index
        return {c: (1 / len(top) if c in top else 0.0) for c in returns.columns}


class RiskParity(_PortfolioLevel):
    """Inverse-volatility weights (naive risk parity) so each asset contributes similar risk."""
    code, name = "quant_risk_parity", "Risk Parity"

    @staticmethod
    def allocate(returns: pd.DataFrame) -> dict[str, float]:
        iv = 1 / returns.iloc[-100:].std().replace(0, np.nan)
        w = iv / iv.sum()
        return {k: float(v) for k, v in w.fillna(0).items()}


FEATURES = ["ret_1", "ret_5", "ret_20", "rsi", "atr_pct", "adx", "macd_hist", "bb_pos", "ema_gap"]


def ml_features(df: pd.DataFrame) -> pd.DataFrame:
    """Causal feature matrix used by the ML strategy (and its offline trainer)."""
    c = df["close"]
    bb = ind.bollinger(c)
    return pd.DataFrame({
        "ret_1": c.pct_change(), "ret_5": c.pct_change(5), "ret_20": c.pct_change(20),
        "rsi": ind.rsi(c) / 100, "atr_pct": ind.atr(df) / c, "adx": ind.adx(df)["adx"] / 100,
        "macd_hist": ind.macd(c)["hist"] / c, "bb_pos": (c - bb["lower"]) / (bb["upper"] - bb["lower"]),
        "ema_gap": (ind.ema(c, 20) - ind.ema(c, 50)) / c,
    }, index=df.index)


class MachineLearning(Strategy):
    """Validated classifier (aux['model'] with predict_proba) on causal features.
    Requires an admin-approved model artefact; never trains or swaps models in production."""
    code, name, category, component = "quant_ml", "Machine Learning Trading", CAT, C_INDICATOR
    requires = [CAP_MODEL]
    params = {"threshold": 0.6}

    def detect(self, ctx):
        model = ctx.aux.get(CAP_MODEL)
        if model is None:
            return None
        x = ml_features(ctx.df).iloc[[-1]][FEATURES]
        if x.isna().any(axis=None):
            return None
        p_up = float(model.predict_proba(x.values)[0][1])
        if p_up >= self.p["threshold"]:
            return self.make(ctx, "BUY", None, strength=p_up, reasons=[f"Model P(up)={p_up:.2f}"])
        if p_up <= 1 - self.p["threshold"]:
            return self.make(ctx, "SELL", None, strength=1 - p_up, reasons=[f"Model P(up)={p_up:.2f}"])
        return None


class AISignal(Strategy):
    """Claude agent review of shortlisted candidates. Runs in the agent layer (app.agents), not
    per bar; it can only confirm/downgrade/reject candidates and never bypasses the Risk Engine."""
    code, name, category, component = "quant_ai_signal", "AI Signal Trading", CAT, C_INDICATOR
    requires = [CAP_AGENT]

    def detect(self, ctx):
        return None


class HighFrequency(Strategy):
    """Sub-second microstructure trading. Requires co-located low-latency execution (unavailable
    through a retail MT5 terminal), so this module is reported as unavailable."""
    code, name, category, component = "quant_hft", "High-Frequency Trading", CAT, C_INDICATOR
    requires = [CAP_HFT, CAP_DOM]
    default_enabled = False

    def detect(self, ctx):
        book = ctx.aux.get(CAP_DOM)
        if not book:
            return None
        bid_sz = sum(l["size"] for l in book if l["side"] == "bid")
        ask_sz = sum(l["size"] for l in book if l["side"] == "ask")
        imb = (bid_sz - ask_sz) / max(bid_sz + ask_sz, 1)
        if imb > 0.6:
            return self.make(ctx, "BUY", None, rr=1.0, reasons=[f"Book imbalance {imb:.2f}"])
        if imb < -0.6:
            return self.make(ctx, "SELL", None, rr=1.0, reasons=[f"Book imbalance {imb:.2f}"])
        return None


class MarketMaking(Strategy):
    """Two-sided quoting with inventory skew. Requires DOM + HFT infrastructure (unavailable)."""
    code, name, category, component = "quant_market_making", "Market Making", CAT, C_INDICATOR
    requires = [CAP_HFT, CAP_DOM]
    default_enabled = False

    def detect(self, ctx):
        inv = float(ctx.aux.get("inventory", 0))
        if not ctx.aux.get(CAP_DOM):
            return None
        return self.make(ctx, "SELL" if inv > 0 else "BUY", None, rr=0.5, reasons=[f"Inventory skew {inv:+.2f}"])


class VolatilityArbitrage(Strategy):
    """Implied vs realised volatility spread. Needs an options IV surface (not available from
    MT5 spot/CFD feeds), so it cannot be expressed or traded here; reported unavailable."""
    code, name, category, component = "quant_vol_arb", "Volatility Arbitrage", CAT, C_INDICATOR
    requires = [CAP_IV]
    default_enabled = False

    def detect(self, ctx):
        return None


class BreakoutAlgo(Strategy):
    """Volatility-adjusted breakout: close > N-bar high + 0.25 ATR during expanding ATR."""
    code, name, category, component = "quant_breakout", "Breakout Algorithm", CAT, C_TREND
    params = {"n": 30, "buffer_atr": 0.25, "rr": 2.0}
    regimes = ["BREAKOUT", "CONSOLIDATION"]

    def detect(self, ctx):
        a = ctx.atr()
        hi = ctx.df.high.iloc[-self.p["n"] - 1:-1].max()
        lo = ctx.df.low.iloc[-self.p["n"] - 1:-1].min()
        buf = self.p["buffer_atr"] * a.iloc[-1]
        expanding = a.iloc[-1] > a.iloc[-10:].mean()
        c = ctx.last()
        if c > hi + buf and expanding:
            return self.make(ctx, "BUY", float(hi - buf), reasons=["Vol-adjusted breakout up"])
        if c < lo - buf and expanding:
            return self.make(ctx, "SELL", float(lo + buf), reasons=["Vol-adjusted breakout down"])
        return None


class Cointegration(Strategy):
    """Engle-Granger: trade the spread only if residual ADF t-stat < -3.37 (≈5% for 2 series)."""
    code, name, category, component = "quant_cointegration", "Cointegration Trading", CAT, C_INDICATOR
    requires = [CAP_PEERS]
    params = {"window": 250, "z": 2.0, "crit": -3.37}
    min_bars = 260

    def detect(self, ctx):
        p = _peer(ctx)
        if p is None:
            return None
        w = self.p["window"]
        y, x = np.log(ctx.close.iloc[-w:].values), np.log(p.iloc[-w:].values)
        a, b = ols_beta(y, x)
        resid = y - (a + b * x)
        t = adf_tstat(resid)
        if t > self.p["crit"]:
            return None
        z = (resid[-1] - resid.mean()) / resid.std()
        if z < -self.p["z"]:
            return self.make(ctx, "BUY", None, reasons=[f"Cointegrated (ADF t={t:.2f}), z={z:.2f}"])
        if z > self.p["z"]:
            return self.make(ctx, "SELL", None, reasons=[f"Cointegrated (ADF t={t:.2f}), z={z:.2f}"])
        return None


class AdaptiveSystem(Strategy):
    """Kaufman efficiency ratio switches between trend mode (KAMA cross) and range mode (z-fade)."""
    code, name, category, component = "quant_adaptive", "Adaptive Trading Systems", CAT, C_TREND
    params = {"n": 10, "er_trend": 0.35, "er_range": 0.2}

    def detect(self, ctx):
        c = ctx.close
        n = self.p["n"]
        change = (c - c.shift(n)).abs()
        vol = c.diff().abs().rolling(n).sum()
        er = (change / vol.replace(0, np.nan)).fillna(0)
        sc = (er * (2 / 3 - 2 / 31) + 2 / 31) ** 2
        kama = np.full(len(c), np.nan)
        start = n
        kama[start] = c.iloc[start]
        cv, scv = c.to_numpy(float), sc.to_numpy(float)
        for i in range(start + 1, len(c)):
            kama[i] = kama[i - 1] + scv[i] * (cv[i] - kama[i - 1])
        k = pd.Series(kama, index=c.index)
        e = er.iloc[-1]
        if e >= self.p["er_trend"]:
            if c.iloc[-2] <= k.iloc[-2] and c.iloc[-1] > k.iloc[-1]:
                return self.make(ctx, "BUY", None, reasons=[f"Trend mode (ER {e:.2f}) KAMA cross up"])
            if c.iloc[-2] >= k.iloc[-2] and c.iloc[-1] < k.iloc[-1]:
                return self.make(ctx, "SELL", None, reasons=[f"Trend mode (ER {e:.2f}) KAMA cross down"])
        elif e <= self.p["er_range"]:
            z = ind.zscore(c, 30).iloc[-1]
            cd = candle(ctx)
            if z < -2 and cd["bull"]:
                return self.make(ctx, "BUY", None, target=float(k.iloc[-1]), reasons=[f"Range mode (ER {e:.2f}) fade"])
            if z > 2 and cd["bear"]:
                return self.make(ctx, "SELL", None, target=float(k.iloc[-1]), reasons=[f"Range mode (ER {e:.2f}) fade"])
        return None


STRATEGIES = [StatArb, PairsTrading, MeanReversionAlgo, MomentumAlgo, TrendFollowingAlgo, GridTrading, Martingale,
              AntiMartingale, DCA, PortfolioRebalancing, MachineLearning, AISignal, HighFrequency, MarketMaking,
              BreakoutAlgo, VolatilityArbitrage, Cointegration, FactorInvesting, RiskParity, AdaptiveSystem]
