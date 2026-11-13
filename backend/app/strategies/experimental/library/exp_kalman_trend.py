import numpy as np


class KalmanVelocityTrend(Strategy):
    """Constant-velocity Kalman filter on log price. Enter when the filtered velocity crosses zero with a
    signal-to-noise ratio (velocity / its std) above a threshold; the stop sits 2 ATR away."""
    code, name, category = "exp_kalman_trend", "Kalman Filter Velocity Trend", "experimental"
    component = "trend"
    params = {"q": 1e-6, "r_mult": 1.0, "snr": 2.0, "sl_atr": 2.0, "rr": 2.0}
    min_bars = 200

    def filt(self, y):
        F = np.array([[1.0, 1.0], [0.0, 1.0]])
        H = np.array([[1.0, 0.0]])
        Q = self.p["q"] * np.array([[0.25, 0.5], [0.5, 1.0]])
        R = np.array([[np.var(np.diff(y)) * self.p["r_mult"] + 1e-12]])
        x = np.array([y[0], 0.0])
        P = np.eye(2)
        vel, var = [], []
        for z in y:
            x = F @ x
            P = F @ P @ F.T + Q
            S = H @ P @ H.T + R
            K = P @ H.T / S
            x = x + (K * (z - (H @ x))).ravel()
            P = (np.eye(2) - K @ H) @ P
            vel.append(x[1])
            var.append(P[1, 1])
        return np.array(vel), np.sqrt(np.array(var))

    def detect(self, ctx):
        y = np.log(ctx.close.to_numpy(float))[-300:]
        v, sd = self.filt(y)
        snr_now, snr_prev = v[-1] / sd[-1], v[-2] / sd[-2]
        t = self.p["snr"]
        if snr_prev <= t < snr_now:
            return self.make(ctx, "BUY", None, reasons=[f"Kalman velocity SNR crossed +{t} ({snr_now:.2f})"])
        if snr_prev >= -t > snr_now:
            return self.make(ctx, "SELL", None, reasons=[f"Kalman velocity SNR crossed -{t} ({snr_now:.2f})"])
        return None
