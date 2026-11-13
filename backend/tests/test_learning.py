"""Part 25 data foundation: deal collection, aggregation, enrichment, performance, failure, memory."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.models import ClosedPosition, TradingAccount
from app.db.models_intel import MT5Deal, TradeFeatures
from app.db.session import session_scope
from app.execution import manager as em
from app.learning_engine import collector, failure, features, memory, performance as perf
from app.services import trading as tr
from app.workers import main as worker
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)


def d(ticket, pos, entry, typ, vol, price, profit=0.0, comm=-0.5, minutes=0, reason=-1):
    return SimpleNamespace(deal_ticket=ticket, position_id=pos, entry=entry, deal_type=typ, volume=vol, price=price,
                           profit=profit, commission=comm, fee=0.0, swap=0.0, deal_time=T0 + timedelta(minutes=minutes),
                           reason=reason, symbol="EURUSD", comment="")


def test_aggregate_partial_exits_vwap_and_costs():
    deals = [d(1, 9, 0, 0, 1.0, 1.1000), d(2, 9, 1, 1, 0.4, 1.1050, profit=200, minutes=30),
             d(3, 9, 1, 1, 0.6, 1.1020, profit=120, minutes=60, reason=5)]
    a = collector.aggregate_position(deals)
    assert a["direction"] == "BUY" and a["volume"] == 1.0 and a["partial_exits"] == 2 and a["complete"]
    assert a["close_price"] == pytest.approx((0.4 * 1.1050 + 0.6 * 1.1020) / 1.0)
    assert a["profit"] == 320 and a["commission"] == pytest.approx(-1.5) and a["close_reason"] == "TAKE_PROFIT"
    # still partially open -> not complete
    assert collector.aggregate_position(deals[:2])["complete"] is False
    assert collector.aggregate_position(deals[:1]) is None


def test_aggregate_flags_a_ledger_that_does_not_reconcile():
    entry = d(1, 9, 0, 0, 1.0, 1.1000, minutes=10)
    assert collector.aggregate_position([entry, d(2, 9, 1, 1, 1.0, 1.1050, minutes=20)])["problems"] == []
    over = collector.aggregate_position([entry, d(2, 9, 1, 1, 1.0, 1.1050, minutes=20), d(3, 9, 1, 1, 0.5, 1.2, minutes=30)])
    assert any("exit volume" in p for p in over["problems"])
    early = collector.aggregate_position([entry, d(2, 9, 1, 1, 1.0, 1.1050, minutes=0)])
    assert any("precedes" in p for p in early["problems"])
    same_side = collector.aggregate_position([entry, d(2, 9, 1, 0, 1.0, 1.1050, minutes=20)])
    assert any("same side" in p for p in same_side["problems"])


def test_reused_position_id_is_flagged_and_never_learned_from(stack):
    """A ledger that stops reconciling (e.g. a position id reused after a terminal reset) must not overwrite
    the recorded outcome or produce an R multiple; the position is excluded from learning."""
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="reuse-1")
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        em.manage(db, bridge, action="CLOSE", position_ticket=ticket, dedupe_key="reuse-close")
        collector.sync_account_history(db, bridge, tr.active_account(db))
        cp = db.scalar(select(ClosedPosition).where(ClosedPosition.ticket == ticket))
        before = (float(cp.profit), cp.r_multiple)
        assert len(perf.load_trades(db, "DEMO")) == 1
    stray = dict(fake.deals[-1], ticket=fake.deals[-1]["ticket"] + 1000, volume=0.5)  # extra exit, same position id
    fake.deals.append(stray)
    with session_scope() as db:
        r = collector.sync_account_history(db, bridge, tr.active_account(db))
        assert r.get("inconsistent") == 1 and not r.get("corrected")
        cp = db.scalar(select(ClosedPosition).where(ClosedPosition.ticket == ticket))
        assert cp.sync_status == "INCONSISTENT" and (float(cp.profit), cp.r_multiple) == before
        assert perf.load_trades(db, "DEMO") == []


def test_metrics_are_correct_and_flag_small_samples():
    trades = [{"net": v, "r": v / 100, "closed_at": T0 + timedelta(days=i)} for i, v in enumerate([100, -50, 100, -50])]
    m = perf.compute_metrics(trades)
    assert m["win_rate"] == 0.5 and m["net_profit"] == 100 and m["profit_factor"] == 2.0
    assert m["expectancy"] == 25 and m["max_drawdown"] == 50 and m["verdict"] == "INSUFFICIENT_DATA"
    big = [{"net": 10.0 if i % 3 else -5.0, "r": None, "closed_at": T0 + timedelta(hours=i)} for i in range(90)]
    m2 = perf.compute_metrics(big)
    assert m2["verdict"] == "POSITIVE_EDGE" and m2["expectancy_ci95"][0] > 0
    lo, hi = perf.wilson(60, 90)
    assert lo < 60 / 90 < hi


def test_sync_imports_deals_once_aggregates_and_separates_modes(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        o = open_eur(db, bridge, key="learn-1")
        assert o.status == "CONFIRMED"
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        em.manage(db, bridge, action="PARTIAL_CLOSE", position_ticket=ticket, dedupe_key="pc-1", volume=0.1)
    with session_scope() as db:
        em.manage(db, bridge, action="CLOSE", position_ticket=ticket, dedupe_key="cl-1")
    with session_scope() as db:
        acct = tr.active_account(db)
        r1 = collector.sync_account_history(db, bridge, acct)
        assert r1["ok"] and r1["inserted"] == 3
    with session_scope() as db:
        acct = tr.active_account(db)
        r2 = collector.sync_account_history(db, bridge, acct)
        assert r2["inserted"] == 0                      # incremental re-sync never duplicates
        assert db.scalar(select(func.count(MT5Deal.id))) == 3
        cp = db.scalar(select(ClosedPosition).where(ClosedPosition.ticket == ticket))
        assert cp.partial_exits == 2 and cp.mode == "DEMO" and cp.execution_id == o.execution_id
        assert cp.r_multiple is not None
        tfeat = features.enrich(db, cp, bridge)
        assert tfeat.outcome in ("WIN", "LOSS", "BE") and tfeat.mode == "DEMO" and tfeat.initial_risk > 0
        assert perf.load_trades(db, "LIVE") == []        # DEMO data never leaks into LIVE analysis
        assert len(perf.load_trades(db, "DEMO")) == 1


def test_broker_correction_updates_and_marks_record(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="corr-1")
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        em.manage(db, bridge, action="CLOSE", position_ticket=ticket, dedupe_key="corr-close")
        collector.sync_account_history(db, bridge, tr.active_account(db))
    fake.deals[-1]["profit"] = 99.0                        # broker corrects the exit deal
    with session_scope() as db:
        r = collector.sync_account_history(db, bridge, tr.active_account(db))
        assert r["updated"] == 1 and r.get("corrected") == 1
        cp = db.scalar(select(ClosedPosition).where(ClosedPosition.ticket == ticket))
        assert cp.sync_status == "CORRECTED" and float(cp.profit) == pytest.approx(99.0)


def test_failure_analysis_memory_and_evidence(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="fail-1")
    ticket = next(iter(fake.positions))
    fake.deals_profit_override = None
    with session_scope() as db:
        em.manage(db, bridge, action="CLOSE", position_ticket=ticket, dedupe_key="fail-close")
        collector.sync_account_history(db, bridge, tr.active_account(db))
    with session_scope() as db:
        cp = db.scalar(select(ClosedPosition).where(ClosedPosition.ticket == ticket))
        cp.profit, cp.r_multiple, cp.close_reason = -50.0, -1.0, "STOP_LOSS"
        features.enrich(db, cp, None)
        res = failure.run(db, "DEMO")
        assert res["analysed"] == 1
        perf.snapshot(db, "DEMO")
        ev = memory.retrieve_evidence(db, "DEMO", "EURUSD.r", "H1", [], "SIDEWAYS")
        assert ev["performance"]["symbol:EURUSD.r"]["sample_size"] == 1
        assert ev["performance"]["symbol:EURUSD.r"]["verdict"] == "INSUFFICIENT_DATA"
        assert ev["recent_trades_same_symbol"][0]["outcome"] == "LOSS"
        assert ev["loss_causes_same_symbol"]


def test_analyse_trade_rules():
    cp = SimpleNamespace(strategy_codes=["pa_breakout"], close_reason="STOP_LOSS", stop_loss=1.09, open_price=1.10,
                         direction="BUY", r_multiple=-1.0)
    tf = SimpleNamespace(outcome="LOSS", regime="STRONG_BULL", grade="B", confluence_score=58, mfe_r=0.1, mae_r=1.0,
                         news_status="UNKNOWN", spread_points=5, slippage_points=1, session="LONDON")
    res = failure.analyse_trade(cp, tf, predicted_p=0.7)
    names = {c["cause"] for c in res["causes"]}
    assert {"incorrect_regime", "weak_confluence", "false_breakout", "poor_entry_timing", "news_volatility",
            "model_overconfidence"} <= names
    assert all(c["evidence"] for c in res["causes"])
_ = TradingAccount, TradeFeatures
