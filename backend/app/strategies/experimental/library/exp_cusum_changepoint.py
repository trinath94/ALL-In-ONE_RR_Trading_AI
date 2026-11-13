import numpy as np


class CusumChangePointMomentum(Strategy):
    """Two-sided CUSUM on standardised returns. A fresh positive (negative) alarm marks a change point in
    mean return; enter in the direction of the shift if price confirms above (below) EMA20."""
    code, name, category = "exp_cusum_changepoint", "CUSUM Change-Point Momentum", "experimental"
    component = "trend"
    params = {"threshold": 4.0, "drift": 0.5, "lookback": 200, "sl_atr": 1.8, "rr": 2.0}
    min_bars = 220

    def detect(self, ctx):
        r = np.diff(np.log(ctx.close.to_numpy(float)))[-self.p["lookback"]:]
        z = (r - r[:-20].mean()) / (r[:-20].std() or 1.0)
        gp = gn = 0.0
        alarm = None
        for i, v in enumerate(z):
            gp = max(0.0, gp + v - self.p["drift"])
            gn = max(0.0, gn - v - self.p["drift"])
            if gp > self.p["threshold"]:
                alarm, gp, gn = ("UP", i), 0.0, 0.0
            elif gn > self.p["threshold"]:
                alarm, gp, gn = ("DOWN", i), 0.0, 0.0
        if alarm is None or alarm[1] != len(z) - 1:
            return None
        e20 = ctx.ema(20).iloc[-1]
        price = ctx.last()
        if alarm[0] == "UP" and price > e20:
            return self.make(ctx, "BUY", None, reasons=["CUSUM upward change point"])
        if alarm[0] == "DOWN" and price < e20:
            return self.make(ctx, "SELL", None, reasons=["CUSUM downward change point"])
        return None
