from datetime import datetime, timedelta, timezone

import pytest

from app.risk import engine as r

UTC = timezone.utc

EURUSD = r.SymbolSpec("EURUSD", 5, 1e-5, 1e-5, 1.0, 100_000, 0.01, 100, 0.01, stops_level=10,
                      profit_currency="USD", base_currency="EUR")
XAUUSD = r.SymbolSpec("XAUUSD", 2, 0.01, 0.01, 1.0, 100, 0.01, 50, 0.01, stops_level=50,
                      profit_currency="USD", base_currency="XAU")
USDJPY = r.SymbolSpec("USDJPY", 3, 0.001, 0.001, 0.6667, 100_000, 0.01, 100, 0.01, profit_currency="JPY",
                      base_currency="USD")


def acct(equity=10_000, **kw):
    d = dict(login=1, server="S", mode="DEMO", currency="USD", balance=equity, equity=equity, margin=0,
             free_margin=equity, leverage=100, day_start_equity=equity, week_start_equity=equity,
             month_start_equity=equity, peak_equity=equity)
    d.update(kw)
    return r.AccountState(**d)


def req(symbol="EURUSD", d="BUY", entry=1.10000, sl=1.09800, tp=1.10400, spread=10, **kw):
    kw.setdefault("margin_required_per_lot", 1100)
    return r.OrderRequest(symbol, d, entry, sl, tp, spread, **kw)


P = r.RiskParams(news_unknown_blocks=False)
CLEAR = r.ControlState(news_status="CLEAR")


def test_eurusd_sizing_exact():
    # 0.5% of 10k = 50 USD. Stop 200pt + spread 10 + slippage 0 = 210 ticks * $1 = $2.10/lot -> 23.80 -> 23.8? no:
    d = r.evaluate(req(), EURUSD, acct(), [], P, CLEAR)
    assert d.approved, d.reasons
    # loss per lot = 210 ticks * 1.0 = 210 USD; 50 / 210 = 0.238 -> floored to step 0.01
    assert d.volume == pytest.approx(0.23)
    assert d.risk_amount <= 50 + 1e-9


def test_gold_and_jpy_use_their_own_tick_values():
    g = r.evaluate(req("XAUUSD", entry=2400.00, sl=2390.00, tp=2420.0, spread=30), XAUUSD, acct(), [], P, CLEAR)
    # stop 10.00 + 0.30 spread = 1030 ticks * $1 = $1030/lot -> 50/1030 = 0.048 -> 0.04
    assert g.approved and g.volume == pytest.approx(0.04)
    j = r.evaluate(req("USDJPY", entry=150.000, sl=149.700, tp=150.600, spread=10), USDJPY, acct(), [], P, CLEAR)
    # 300+10 ticks * 0.6667 = 206.7/lot -> 0.2419 -> 0.24
    assert j.approved and j.volume == pytest.approx(0.24)


def test_missing_tick_value_without_conversion_is_rejected():
    spec = r.SymbolSpec("ABC", 2, 0.01, 0.01, 0.0, 10, 0.1, 10, 0.1)
    d = r.evaluate(req("ABC", entry=100, sl=99, tp=102), spec, acct(), [], P, CLEAR)
    assert not d.approved and any("tick" in x.lower() for x in d.reasons)
    d2 = r.evaluate(req("ABC", entry=100, sl=99, tp=102, conversion_rate=1.0), spec, acct(), [], P, CLEAR)
    assert d2.approved


@pytest.mark.parametrize("sl,expect", [(None, "mandatory"), (1.1010, "wrong side"), (1.09995, "stops level")])
def test_stop_loss_validation(sl, expect):
    d = r.evaluate(req(sl=sl), EURUSD, acct(), [], P, CLEAR)
    assert not d.approved and any(expect in x.lower() for x in d.reasons)


def test_volume_below_broker_minimum_is_rejected_not_rounded_up():
    d = r.evaluate(req(sl=1.09), EURUSD, acct(equity=500), [], P, CLEAR)
    assert not d.approved and any("minimum" in x for x in d.reasons)


def test_daily_loss_limit_locks_trading():
    d = r.evaluate(req(), EURUSD, acct(equity=9_790, day_start_equity=10_000, week_start_equity=10_000,
                                       month_start_equity=10_000, peak_equity=10_000), [], P, CLEAR)
    assert not d.approved and d.lock_trading and "daily" in d.lock_reason


def test_drawdown_and_consecutive_losses():
    d = r.evaluate(req(), EURUSD, acct(equity=8_900, day_start_equity=8_900, week_start_equity=8_900,
                                       month_start_equity=8_900, peak_equity=10_000), [], P, CLEAR)
    assert not d.approved and "drawdown" in d.lock_reason
    d2 = r.evaluate(req(), EURUSD, acct(consecutive_losses=4), [], P, CLEAR)
    assert not d2.approved and d2.lock_trading


def test_kill_switch_pause_and_news():
    assert not r.evaluate(req(), EURUSD, acct(), [], P, r.ControlState(kill_switch=True, news_status="CLEAR")).approved
    assert not r.evaluate(req(), EURUSD, acct(), [], P, r.ControlState(pause_new_entries=True, news_status="CLEAR")).approved
    assert not r.evaluate(req(), EURUSD, acct(), [], P, r.ControlState(news_status="RESTRICTED")).approved
    strict = r.RiskParams()  # default: unknown news blocks
    d = r.evaluate(req(), EURUSD, acct(), [], strict, r.ControlState(news_status="UNKNOWN"))
    assert not d.approved and any("UNKNOWN" in x for x in d.reasons)


def test_spread_and_expiry():
    assert not r.evaluate(req(spread=80), EURUSD, acct(), [], P, CLEAR).approved
    past = datetime.now(UTC) - timedelta(minutes=1)
    assert not r.evaluate(req(expires_at=past), EURUSD, acct(), [], P, CLEAR).approved


def test_duplicate_and_max_positions():
    pos = [r.OpenRisk("EURUSD", "BUY", 0.2, 40, 22_000)]
    d = r.evaluate(req(), EURUSD, acct(), pos, P, CLEAR)
    assert not d.approved and any("already open" in x or "max" in x for x in d.reasons)
    many = [r.OpenRisk(f"S{i}", "BUY", 0.01, 1, 100) for i in range(5)]
    assert not r.evaluate(req(), EURUSD, acct(), many, P, CLEAR).approved


def test_correlated_exposure_via_currency_legs():
    # Long EURJPY already risks 1.4% -> long EURUSD shares the long-EUR leg
    pos = [r.OpenRisk("EURJPY", "BUY", 0.5, 140, 50_000)]
    d = r.evaluate(req(), EURUSD, acct(), pos, P, CLEAR)
    assert not d.approved and any("correlated" in x for x in d.reasons)
    # explicit correlation matrix: short USDCHF ~ long EURUSD (rho -0.9)
    pos2 = [r.OpenRisk("USDCHF", "SELL", 0.5, 140, 50_000)]
    d2 = r.evaluate(req(), EURUSD, acct(), pos2, P, CLEAR, corr={("EURUSD", "USDCHF"): -0.9})
    assert not d2.approved


def test_margin_validation():
    d = r.evaluate(req(margin_required_per_lot=None), EURUSD, acct(), [], P, CLEAR)
    assert not d.approved and any("margin" in x.lower() for x in d.reasons)
    d2 = r.evaluate(req(margin_required_per_lot=30_000), EURUSD, acct(), [], P, CLEAR)
    assert not d2.approved and any("free margin" in x for x in d2.reasons)


def test_martingale_multiplier_is_capped():
    d = r.evaluate(req(size_multiplier=8), EURUSD, acct(), [], P, CLEAR)
    assert d.approved and d.volume == pytest.approx(0.23)  # capped to x1


def test_floor_to_step_never_rounds_up():
    assert r.floor_to_step(0.239999, 0.01) == pytest.approx(0.23)
    assert r.floor_to_step(1.0, 0.1) == pytest.approx(1.0)


def test_params_validation():
    p = r.RiskParams(max_daily_loss_pct=5, max_weekly_loss_pct=4)
    assert p.validate()
    assert r.RiskParams().validate() == []


def test_emergency_policies():
    pv = [r.PositionView(1, "EURUSD", "BUY", 0.2, 1.1, 1.105, None, None),
          r.PositionView(2, "XAUUSD", "SELL", 0.1, 2400, 2390, 2410, 2350)]
    prot = r.emergency_actions("PROTECT", pv, P, {"EURUSD": 0.001})
    assert prot == [{"action": "MODIFY", "ticket": 1, "symbol": "EURUSD", "stop_loss": pytest.approx(1.103)}]
    red = r.emergency_actions("REDUCE", pv, P, {"EURUSD": 0.001})
    assert sum(1 for a in red if a["action"] == "PARTIAL_CLOSE") == 2
    assert all(a["action"] == "CLOSE" for a in r.emergency_actions("CLOSE_ALL", pv, P))


def test_break_even_and_trailing():
    p = r.PositionView(1, "EURUSD", "BUY", 0.2, 1.1000, 1.1022, 1.0980, None, initial_stop=1.0980)
    acts = r.manage_position(p, P, atr=0.0005, spec=EURUSD)
    assert any(a["action"] == "PARTIAL_CLOSE" for a in acts)  # 1.1R >= partial at 1R
    assert any(a.get("reason") == "break-even" for a in acts)
    p2 = r.PositionView(1, "EURUSD", "BUY", 0.2, 1.1000, 1.1040, 1.1000, None, initial_stop=1.0980, partial_done=True)
    acts2 = r.manage_position(p2, P, atr=0.0005, spec=EURUSD)
    assert acts2 and acts2[-1]["reason"] == "trailing" and acts2[-1]["stop_loss"] == pytest.approx(1.1030)
