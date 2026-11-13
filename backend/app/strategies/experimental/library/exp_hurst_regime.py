import math

import numpy as np


class HurstRegimeSwitch(Strategy):
    """Rescaled-range Hurst exponent selects the playbook: H > 0.55 (persistent) -> trade EMA20 pullbacks in
    trend direction; H < 0.45 (anti-persistent) -> fade 2-sigma z-score extremes back to the mean."""
    code, name, category = "exp_hurst_regime", "Hurst Exponent Regime Switch", "experimental"
    component = "indicator"
    params = {"lookback": 256, "h_trend": 0.55, "h_mr": 0.45, "rr": 2.0}
    min_bars = 280

    @staticmethod
    def hurst(x):
        x = np.asarray(x, float)
        rs, ns = [], []
        for n in (16, 32, 64, 128):
            if len(x) < 2 * n:
                break
            vals = []
            for k in range(len(x) // n):
                c = x[k * n:(k + 1) * n]
                y = np.cumsum(c - c.mean())
                r, s = y.max() - y.min(), c.std()
                if s > 0:
                    vals.append(r / s)
            if vals:
                rs.append(math.log(np.mean(vals)))
                ns.append(math.log(n))
        if len(ns) < 3:
            return None
        return float(np.polyfit(ns, rs, 1)[0])

    def detect(self, ctx):
        r = np.diff(np.log(ctx.close.to_numpy(float)))[-self.p["lookback"]:]
        h = self.hurst(r)
        if h is None:
            return None
        c = candle(ctx)
        e20, e50 = ctx.ema(20).iloc[-1], ctx.ema(50).iloc[-1]
        if h > self.p["h_trend"]:
            if e20 > e50 and c["l"] <= e20 < c["c"]:
                return self.make(ctx, "BUY", None, reasons=[f"Hurst {h:.2f} persistent: EMA20 pullback long"])
            if e20 < e50 and c["h"] >= e20 > c["c"]:
                return self.make(ctx, "SELL", None, reasons=[f"Hurst {h:.2f} persistent: EMA20 pullback short"])
        elif h < self.p["h_mr"]:
            m, sd = ctx.close.iloc[-50:].mean(), ctx.close.iloc[-50:].std()
            z = (c["c"] - m) / sd if sd > 0 else 0
            if z < -2 and c["bull"]:
                return self.make(ctx, "BUY", None, target=float(m), reasons=[f"Hurst {h:.2f} mean-reverting, z={z:.1f}"])
            if z > 2 and c["bear"]:
                return self.make(ctx, "SELL", None, target=float(m), reasons=[f"Hurst {h:.2f} mean-reverting, z={z:.1f}"])
        return None
