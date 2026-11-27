"""Trader discipline: stop after a bad day, smaller size after a losing streak, key levels, A+ setups only.
Everything only holds back or reduces; nothing forces a trade, and the defaults (0) keep the original behaviour."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.db.models import AnalysisResult, RiskConfig, TradeOrder
from app.db.session import session_scope
from app.execution.manager import ExecutionBlocked
from app.intelligence import levels
from app.risk import engine as r
from app.risk.presets import PRESETS
from tests.test_engine_first import NoAI, run_scan
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)
from tests.test_risk import CLEAR, EURUSD, acct, req

UTC = timezone.utc


def test_defaults_are_off_and_keep_the_original_behaviour():
    p = r.RiskParams(news_unknown_blocks=False)
    d = r.evaluate(req(), EURUSD, acct(trades_today=50, losses_today=9, consecutive_losses=3), [], p, CLEAR)
    assert d.approved and not {"daily_trades", "daily_losses", "losing_streak_sizing"} & {c.name for c in d.checks}


def test_done_for_the_day_after_the_daily_loss_count_and_trade_counts():
    p = r.RiskParams(news_unknown_blocks=False, max_losses_per_day=3, max_trades_per_day=5, max_trades_per_symbol_day=2)
    assert r.evaluate(req(), EURUSD, acct(losses_today=2, trades_today=4), [], p, CLEAR).approved
    d = r.evaluate(req(), EURUSD, acct(losses_today=3), [], p, CLEAR)
    assert not d.approved and any("done for the day" in x for x in d.reasons) and not d.lock_trading, \
        "a daily stop, not a risk lock: it resets the next day"
    assert any("trades today" in x for x in r.evaluate(req(), EURUSD, acct(trades_today=5), [], p, CLEAR).reasons)
    assert any("on EURUSD today" in x for x in r.evaluate(req(symbol_trades_today=2), EURUSD, acct(), [], p,
                                                          CLEAR).reasons)
    assert r.evaluate(req(source="SYSTEM"), EURUSD, acct(losses_today=9, trades_today=9), [], p, CLEAR).approved, \
        "protective system orders are never held back"


def test_a_losing_streak_halves_the_size_and_never_raises_it():
    p = r.RiskParams(news_unknown_blocks=False, streak_reduce_after=2, streak_reduce_factor=0.5)
    normal = r.evaluate(req(), EURUSD, acct(consecutive_losses=1), [], p, CLEAR)
    reduced = r.evaluate(req(), EURUSD, acct(consecutive_losses=2), [], p, CLEAR)
    assert reduced.approved and reduced.volume == pytest.approx(0.11) and normal.volume == pytest.approx(0.23)
    assert r.RiskParams(streak_reduce_factor=1.5).validate() and r.RiskParams(min_setup_score=8).validate()


def test_profiles_with_discipline_are_valid():
    for pid in ("ftmo", "pro_discipline"):
        v = PRESETS[pid]["values"]
        assert v["max_losses_per_day"] == 3 and v["min_setup_score"] == 4
        assert not r.RiskParams.from_dict(v).validate()
    assert "max_daily_loss_pct" not in PRESETS["pro_discipline"]["values"], "keeps the user's own loss limits"


def h1_frame():
    idx = pd.date_range("2026-11-23 00:00", "2026-11-27 10:00", freq="h", tz="UTC")
    df = pd.DataFrame({"open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1}, index=idx)
    df.loc["2026-11-26", "high"] = 1.1080                     # previous day
    df.loc["2026-11-26", "low"] = 1.0950
    df.loc["2026-11-27 02:00", "high"] = 1.1030                # Asian session today
    df.loc["2026-11-27 03:00", "low"] = 1.0970
    df.loc["2026-11-27 09:00", "high"] = 1.1060                # London: not part of the Asian range
    return df


def test_key_levels_are_the_ones_a_trader_marks():
    lv = {x["name"]: x["price"] for x in levels.compute(h1_frame(), 1e-5, price=1.1012)}
    assert lv["previous day high"] == 1.108 and lv["previous day low"] == 1.095
    assert lv["Asian range high"] == 1.103 and lv["Asian range low"] == 1.097
    assert lv["week open"] == 1.1
    rounds = sorted(x["price"] for x in levels.compute(h1_frame(), 1e-5, price=1.1012) if x["name"] == "round number")
    assert rounds == [pytest.approx(1.1), pytest.approx(1.105)]
    n = levels.nearest(levels.compute(h1_frame(), 1e-5, 1.1012), 1.1012, atr=0.0010)
    assert n["price"] == pytest.approx(1.1) and n["distance_atr"] == pytest.approx(1.2)
    assert levels.nearest([], 1.1, 0.001) is None and levels.compute(pd.DataFrame(), 1e-5) == []


def set_risk(**kw):
    with session_scope() as db:
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "DEMO"))
        rc.params = {**rc.params, **kw}


def test_weak_setups_are_held_back_before_any_ai_call(scan, monkeypatch):
    set_risk(min_setup_score=7)
    NoAI.calls = 0
    reviews, orders, fake = run_scan(scan, monkeypatch)
    held = [x for x in reviews if x.get("mode") == "QUALITY"]
    assert held and not orders and not fake.positions and NoAI.calls == 0
    assert "setup score" in held[0]["note"] and held[0]["setup"]["max"] == 7
    with session_scope() as db:
        ar = db.scalar(select(AnalysisResult).order_by(AnalysisResult.id.desc()).limit(1))
        assert "levels" in ar.structure["key_levels"], "key levels are recorded with every analysis"
    from app.services import trading as tr
    st = type("S", (), {"auto_trading": True, "auto_trading_since": None, "ai_mode": "AI"})()
    sig = type("G", (), {"decision": "BUY", "created_at": None, "ai_review": held[0]})()
    assert "setup score" in tr.auto_execution_block(st, sig)


def test_with_the_gate_off_the_checklist_is_recorded_and_trading_is_unchanged(scan, monkeypatch):
    reviews, orders, _ = run_scan(scan, monkeypatch)
    scored = [x for x in reviews if x.get("mode") == "ENGINE"]
    assert orders and scored and all("score" in x["setup"] and x["setup"]["block"] is None for x in scored)


def test_daily_trade_limit_through_the_execution_path(stack):
    fake, bridge = stack
    allow_unknown_news()
    set_risk(max_trades_per_day=1, max_trades_per_symbol=3, max_open_positions=5)
    from app.workers import main as worker
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        assert open_eur(db, bridge, key="d1").status == "CONFIRMED"
    with session_scope() as db, pytest.raises(ExecutionBlocked) as e:
        open_eur(db, bridge, key="d2")
    assert "1 trades today" in str(e.value)
    with session_scope() as db:
        assert len(db.scalars(select(TradeOrder)).all()) == 1, "the second entry never reached the broker"
