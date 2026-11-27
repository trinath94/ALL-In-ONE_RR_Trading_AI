"""Named risk profiles that fill the Risk Centre form (the user reviews and saves; every value passes
``RiskParams.validate``). A profile sets only risk limits; account-specific values it does not name (initial capital,
maximum lot size, spread and slippage limits, trade management) keep their current values.

FTMO-style: FTMO's published trading objectives for its Challenge and Verification are a maximum daily loss of 5 % and
a maximum overall loss of 10 % of the initial account balance; its daily loss counts open (floating) losses too. The
engine locks earlier, with buffers (4 % daily, 9 % overall, floating loss 3 %), so a single stop-out or slippage does not
breach the firm's limit. These are the engine's own limits; FTMO's rules for your account type are the ones that count:
check them before trading a challenge.
"""
from __future__ import annotations

PRESETS: dict[str, dict] = {
    "ftmo": {
        "label": "FTMO-style (prop firm: 5 % daily / 10 % overall loss, with buffers)",
        "note": "Locks trading at 4 % daily loss and 9 % drawdown (FTMO: 5 % and 10 % of the initial balance), counts "
                "floating loss (3 %), 0.5 % risk per trade, at most 3 positions and 1 per symbol, reward:risk at least "
                "1.5, take profit required, no entries when news risk is unknown. Set initial capital to your FTMO "
                "account size. Verify the current rules of your FTMO account type.",
        "values": {
            "risk_per_trade_pct": 0.5,
            "max_daily_loss_pct": 4.0,
            "max_weekly_loss_pct": 8.0,
            "max_monthly_loss_pct": 9.0,
            "max_drawdown_pct": 9.0,
            "max_consecutive_losses": 4,
            "max_floating_loss_pct": 3.0,
            "daily_profit_target_pct": 0.0,
            "stop_after_profit_target": False,
            "max_open_positions": 3,
            "max_trades_per_symbol": 1,
            "max_total_exposure_pct": 2.0,
            "max_correlated_exposure_pct": 1.0,
            "max_size_multiplier": 1.0,
            "min_rr": 1.5,
            "require_take_profit": True,
            "news_unknown_blocks": True,
            "min_free_margin_pct": 50.0,
            "existing_position_policy": "PROTECT",
        },
    },
    "engine_default": {
        "label": "Engine default (conservative)",
        "note": "The engine's original limits: 2 % daily, 4 % weekly, 8 % monthly loss, 10 % drawdown, 0.5 % per trade.",
        "values": {
            "risk_per_trade_pct": 0.5, "max_daily_loss_pct": 2.0, "max_weekly_loss_pct": 4.0,
            "max_monthly_loss_pct": 8.0, "max_drawdown_pct": 10.0, "max_consecutive_losses": 4,
            "max_open_positions": 5, "max_trades_per_symbol": 1, "max_total_exposure_pct": 3.0,
            "max_correlated_exposure_pct": 1.5, "max_size_multiplier": 1.0, "min_rr": 1.0,
            "require_take_profit": True, "news_unknown_blocks": True, "max_floating_loss_pct": 0.0,
        },
    },
}
