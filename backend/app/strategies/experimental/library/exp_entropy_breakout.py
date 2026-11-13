import math

import numpy as np


class EntropyCompressionBreakout(Strategy):
    """Shannon entropy of discretised returns falls to a low percentile (orderly, compressed market); a close
    beyond the 20-bar range from that state is taken as an information-driven breakout."""
    code, name, category = "exp_entropy_breakout", "Entropy Compression Breakout", "experimental"
    component = "price_action"
    params = {"window": 50, "bins": 6, "pct": 0.2, "rr": 2.0}
    min_bars = 280

    def entropy(self, r):
        hist = np.histogram(r, bins=self.p["bins"])[0].astype(float)
        p = hist[hist > 0] / hist.sum()
        return float(-(p * np.log(p)).sum() / math.log(self.p["bins"]))

    def detect(self, ctx):
        r = np.diff(np.log(ctx.close.to_numpy(float)))
        w = self.p["window"]
        ent = [self.entropy(r[i - w:i]) for i in range(len(r) - 200, len(r) + 1, 5)]
        if len(ent) < 10:
            return None
        prev = ent[-2]
        if prev > np.quantile(ent[:-1], self.p["pct"]):
            return None
        hi, lo = ctx.df.high.iloc[-21:-1].max(), ctx.df.low.iloc[-21:-1].min()
        c = candle(ctx)
        if c["c"] > hi:
            return self.make(ctx, "BUY", float((hi + lo) / 2), reasons=[f"Low-entropy ({prev:.2f}) range breakout up"])
        if c["c"] < lo:
            return self.make(ctx, "SELL", float((hi + lo) / 2), reasons=[f"Low-entropy ({prev:.2f}) range breakout down"])
        return None
