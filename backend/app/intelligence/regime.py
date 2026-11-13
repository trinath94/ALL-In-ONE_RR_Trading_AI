"""Regime intelligence: probabilities, stability, transitions and strategy suitability.

The original rule-based classifier (``app.analysis.regime.classify``) is kept unchanged and is the
baseline. On top of it:
* a 3-state diagonal Gaussian HMM on (log return, log realised volatility), fitted by Baum-Welch on the
  symbol's own recent history — states are ordered by volatility (LOW/MID/HIGH);
* CUSUM change-point detection on standardised returns and volatility;
* evidence scores for 13 regimes turned into probabilities (softmax), with UNCERTAIN when evidence is
  weak or a change point is fresh.
``benchmark()`` measures whether the HMM volatility state forecasts future volatility better than a plain
ATR-percentile baseline, so the model only earns weight when it adds measurable value.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.analysis import regime as base
from app.analysis import structure as st

MODEL_VERSION = "regime-intel-1.0"
REGIMES = ["STRONG_BULL", "WEAK_BULL", "STRONG_BEAR", "WEAK_BEAR", "SIDEWAYS", "CONSOLIDATION", "HIGH_VOLATILITY",
           "LOW_VOLATILITY", "BREAKOUT", "REVERSAL", "LIQUIDITY_DRIVEN", "NEWS_DRIVEN", "UNCERTAIN"]


# ---------------------------------------------------------------- Gaussian HMM (numpy) --------
@dataclass
class HMM:
    means: np.ndarray
    vars: np.ndarray
    trans: np.ndarray
    start: np.ndarray
    loglik: float = float("nan")


def _emission(X: np.ndarray, means: np.ndarray, vars_: np.ndarray) -> np.ndarray:
    # diagonal Gaussian log densities, shape (T, K)
    diff = X[:, None, :] - means[None, :, :]
    return -0.5 * (np.log(2 * np.pi * vars_)[None, :, :] + diff ** 2 / vars_[None, :, :]).sum(axis=2)


def _forward_backward(logB: np.ndarray, trans: np.ndarray, start: np.ndarray):
    T, K = logB.shape
    B = np.exp(logB - logB.max(axis=1, keepdims=True))
    alpha = np.zeros((T, K))
    c = np.zeros(T)
    alpha[0] = start * B[0]
    c[0] = alpha[0].sum() or 1e-300
    alpha[0] /= c[0]
    for t in range(1, T):
        alpha[t] = (alpha[t - 1] @ trans) * B[t]
        c[t] = alpha[t].sum() or 1e-300
        alpha[t] /= c[t]
    beta = np.ones((T, K))
    for t in range(T - 2, -1, -1):
        beta[t] = (trans @ (B[t + 1] * beta[t + 1])) / c[t + 1]
    gamma = alpha * beta
    gamma /= gamma.sum(axis=1, keepdims=True)
    xi_sum = np.zeros((K, K))
    for t in range(T - 1):
        x = alpha[t][:, None] * trans * (B[t + 1] * beta[t + 1])[None, :] / c[t + 1]
        xi_sum += x / (x.sum() or 1e-300)
    loglik = float(np.log(c).sum() + logB.max(axis=1).sum())
    return gamma, xi_sum, loglik, alpha


def fit_hmm(X: np.ndarray, k: int = 3, iters: int = 40, seed: int = 0) -> HMM:
    rng = np.random.default_rng(seed)
    T, D = X.shape
    q = np.quantile(X[:, -1], np.linspace(0.15, 0.85, k))        # init by volatility quantiles
    means = np.column_stack([np.full(k, X[:, 0].mean())] * (D - 1) + [q]) + rng.normal(0, 1e-6, (k, D))
    vars_ = np.tile(X.var(axis=0) + 1e-8, (k, 1))
    trans = np.full((k, k), 0.05 / (k - 1))
    np.fill_diagonal(trans, 0.95)
    start = np.full(k, 1 / k)
    prev = -np.inf
    ll = prev
    for _ in range(iters):
        gamma, xi, ll, _ = _forward_backward(_emission(X, means, vars_), trans, start)
        w = gamma.sum(axis=0) + 1e-12
        means = (gamma.T @ X) / w[:, None]
        vars_ = np.maximum((gamma.T @ (X ** 2)) / w[:, None] - means ** 2, 1e-8)
        trans = xi / xi.sum(axis=1, keepdims=True)
        start = gamma[0] + 1e-6
        start /= start.sum()
        if ll - prev < 1e-4:
            break
        prev = ll
    order = np.argsort(means[:, -1])                              # sort states by volatility
    return HMM(means[order], vars_[order], trans[np.ix_(order, order)], start[order], ll)


def hmm_features(df: pd.DataFrame, vol_window: int = 10) -> np.ndarray:
    r = np.log(df["close"]).diff()
    rv = r.rolling(vol_window).std()
    X = pd.concat([r, np.log(rv + 1e-10)], axis=1).dropna().to_numpy()
    return X


def hmm_state_probs(model: HMM, X: np.ndarray) -> np.ndarray:
    _, _, _, alpha = _forward_backward(_emission(X, model.means, model.vars), model.trans, model.start)
    return alpha            # filtered (causal) probabilities: uses data up to t only


# ---------------------------------------------------------------- change points ---------------
def cusum_last_change(x: np.ndarray, threshold: float = 5.0, drift: float = 0.5) -> int | None:
    """Two-sided CUSUM on a standardised series; returns bars since the last alarm (or None)."""
    if len(x) < 30:
        return None
    z = (x - np.nanmean(x)) / (np.nanstd(x) or 1.0)
    gp = gn = 0.0
    last = None
    for i, v in enumerate(np.nan_to_num(z)):
        gp = max(0.0, gp + v - drift)
        gn = max(0.0, gn - v - drift)
        if gp > threshold or gn > threshold:
            last = i
            gp = gn = 0.0
    return None if last is None else len(z) - 1 - last


# ---------------------------------------------------------------- regime evidence -------------
def _sig(x: float, c: float, s: float) -> float:
    return float(1 / (1 + np.exp(-(x - c) / s)))


@dataclass
class RegimeIntel:
    regime: str
    probabilities: dict[str, float]
    confidence: float
    stability: float
    transition_warning: str | None
    suitable: list[str] = field(default_factory=list)
    unsuitable: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    model_version: str = MODEL_VERSION

    def dict(self) -> dict:
        return asdict(self)


def analyse(df: pd.DataFrame, news_status: str = "UNKNOWN", strategy_regimes: dict[str, list[str]] | None = None,
            fit_window: int = 500) -> RegimeIntel:
    if len(df) < 120:
        return RegimeIntel("UNCERTAIN", {"UNCERTAIN": 1.0}, 0.0, 0.0, "insufficient history", details={"bars": len(df)})
    b = base.classify(df)                                   # original baseline (unchanged)
    adx = b.adx or 0.0
    slope = b.ema_slope or 0.0
    a = ind.atr(df)
    atr_pct_series = (a / df["close"]).dropna()
    X = hmm_features(df.iloc[-fit_window:])
    hmm_probs = None
    hmm_info = {}
    if len(X) >= 150:
        model = fit_hmm(X)
        p = hmm_state_probs(model, X)
        hmm_probs = p[-1]
        hmm_info = {"state_probs": {"LOW": round(float(hmm_probs[0]), 3), "MID": round(float(hmm_probs[1]), 3),
                                    "HIGH": round(float(hmm_probs[2]), 3)},
                    "stay_prob": round(float(model.trans[int(np.argmax(hmm_probs)), int(np.argmax(hmm_probs))]), 3),
                    "loglik": round(model.loglik, 2)}
    lo_v = float(hmm_probs[0]) if hmm_probs is not None else (1.0 if (b.atr_pct or 0.5) < 0.15 else 0.0)
    hi_v = float(hmm_probs[2]) if hmm_probs is not None else (1.0 if (b.atr_pct or 0.5) > 0.85 else 0.0)
    trend = _sig(adx, 25, 3)
    weak = _sig(adx, 18, 2) * (1 - trend)
    up, dn = _sig(slope, 0.02, 0.01), _sig(-slope, 0.02, 0.01)
    rng_ = 1 - _sig(adx, 20, 3)
    squeeze = 1 - (b.bb_width_pct if b.bb_width_pct is not None else 0.5)
    c = df.iloc[-1]
    prev20 = df.iloc[-21:-1]
    swept = any((df.high.iloc[-k] > prev20.high.max() and df.close.iloc[-k] < prev20.high.max()) or
                (df.low.iloc[-k] < prev20.low.min() and df.close.iloc[-k] > prev20.low.min()) for k in (1, 2, 3))
    ev, _ = st.structure_events(df.iloc[-300:])
    choch_recent = any(e.kind == "CHOCH" and e.idx >= len(df.iloc[-300:]) - 6 for e in ev)
    vol_spike = float(atr_pct_series.iloc[-1] / atr_pct_series.iloc[-50:].median()) if len(atr_pct_series) > 50 else 1.0
    r = np.log(df["close"]).diff().dropna().to_numpy()[-300:]
    cp_ret = cusum_last_change(r)
    cp_vol = cusum_last_change(np.abs(r))
    fresh_cp = min([x for x in (cp_ret, cp_vol) if x is not None], default=None)
    scores = {
        "STRONG_BULL": trend * up * 3, "STRONG_BEAR": trend * dn * 3,
        "WEAK_BULL": weak * up * 2.2, "WEAK_BEAR": weak * dn * 2.2,
        "SIDEWAYS": rng_ * (1 - abs(up - dn)) * 2, "CONSOLIDATION": rng_ * squeeze * 2,
        "HIGH_VOLATILITY": hi_v * 1.6, "LOW_VOLATILITY": lo_v * 1.4,
        "BREAKOUT": (2.2 if "BREAKOUT" in b.tags else 0.0) + (0.6 if abs(c.close - prev20.close.mean()) > 2 * a.iloc[-1] else 0),
        "REVERSAL": 2.0 if choch_recent else (0.8 if "REVERSAL" in b.tags else 0.0),
        "LIQUIDITY_DRIVEN": 1.8 if swept else 0.0,
        "NEWS_DRIVEN": (2.5 if news_status == "RESTRICTED" and vol_spike > 1.5 else 0.0),
        "UNCERTAIN": 0.6 + (1.5 if fresh_cp is not None and fresh_cp <= 5 else 0.0),
    }
    v = np.array([scores[k] for k in REGIMES]) * 2.0
    p = np.exp(v - v.max())
    p /= p.sum()
    probs = {k: round(float(x), 4) for k, x in zip(REGIMES, p)}
    top = max(probs, key=probs.get)
    conf = probs[top]
    if conf < 0.3:
        top = "UNCERTAIN"
    # stability: share of the last 10 bars whose cheap rule label (ADX + slope) equals the current one
    adx_s = ind.adx(df)["adx"].iloc[-10:].to_numpy()
    e50 = ind.ema(df["close"], 50)
    sl = (ind.linreg_slope(e50, 10) / a).iloc[-10:].to_numpy()
    labels = ["T+" if (x >= 25 and y > 0.02) else "T-" if (x >= 25 and y < -0.02) else "R" for x, y in zip(adx_s, sl)]
    stability = labels.count(labels[-1]) / len(labels)
    if hmm_info:
        stability = round(0.5 * stability + 0.5 * hmm_info["stay_prob"], 3)
    warning = None
    if fresh_cp is not None and fresh_cp <= 5:
        warning = f"change point detected {fresh_cp} bars ago (CUSUM)"
    elif stability < 0.5:
        warning = "regime unstable over the last 10 bars"
    second = sorted(probs.items(), key=lambda kv: -kv[1])[1]
    if warning is None and second[1] > 0.8 * conf:
        warning = f"close to {second[0]} ({second[1]:.2f} vs {conf:.2f})"
    suitable, unsuitable = [], []
    tags = {top} | set(b.tags)
    for code, regs in (strategy_regimes or {}).items():
        if not regs:
            continue
        (suitable if tags & set(regs) else unsuitable).append(code)
    return RegimeIntel(top, probs, round(conf, 4), round(float(stability), 3), warning, sorted(suitable), sorted(unsuitable),
                       {"baseline": b.dict(), "hmm": hmm_info, "vol_spike_ratio": round(vol_spike, 3),
                        "cusum_bars_since": {"returns": cp_ret, "volatility": cp_vol}, "liquidity_sweep_recent": swept})


def benchmark(df: pd.DataFrame, horizon: int = 20, train: int = 500) -> dict:
    """Out-of-sample comparison: does HMM P(HIGH vol) rank future realised vol better than ATR percentile?"""
    from scipy.stats import spearmanr
    X = hmm_features(df)
    if len(X) < train + horizon + 100:
        return {"available": False, "reason": f"need >= {train + horizon + 100} bars"}
    model = fit_hmm(X[:train])
    probs = hmm_state_probs(model, X)[:, 2]
    r = np.log(df["close"]).diff().dropna().to_numpy()[-len(X):]
    fut = pd.Series(np.abs(r)).rolling(horizon).mean().shift(-horizon).to_numpy()
    atr_pct = (ind.atr(df) / df["close"]).to_numpy()[-len(X):]
    base_rank = pd.Series(atr_pct).rolling(200, min_periods=50).rank(pct=True).to_numpy()
    sl = slice(train, len(X) - horizon)        # strictly out-of-sample segment
    m = ~np.isnan(fut[sl]) & ~np.isnan(base_rank[sl])
    rho_h = float(spearmanr(probs[sl][m], fut[sl][m]).statistic)
    rho_b = float(spearmanr(base_rank[sl][m], fut[sl][m]).statistic)
    return {"available": True, "oos_bars": int(m.sum()), "hmm_spearman": round(rho_h, 4), "baseline_spearman": round(rho_b, 4),
            "hmm_adds_value": bool(rho_h > max(rho_b, 0.0) + 0.02)}
