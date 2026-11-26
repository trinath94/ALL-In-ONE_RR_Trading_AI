"""Why trades are not executing: the built-in economic calendar (news risk no longer UNKNOWN by default in the installed
engine) and the execution diagnosis from the engine's own records."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import EconomicEvent
from app.db.session import session_scope
from app.execution import manager as em
from app.news import service as news
from app.services import agent, execution_doctor
from app.services import trading as tr
from app.services.events import get_redis
from app.workers import main as worker
from tests.test_execution_e2e import open_eur, stack  # noqa: F401 (fixture)


def feed(now):
    return [
        {"title": "Non-Farm Employment Change", "country": "USD", "date": (now + timedelta(minutes=10)).isoformat(),
         "impact": "High", "forecast": "180K", "previous": "150K"},
        {"title": "German Ifo", "country": "EUR", "date": (now + timedelta(days=2)).astimezone().isoformat(),
         "impact": "Medium"},
        {"title": "no date", "country": "GBP", "impact": "High"},
        {"title": "naive date", "country": "JPY", "date": "2026-11-26T08:30:00", "impact": "High"},
        "garbage",
    ]


def test_calendar_entries_are_parsed_and_bad_ones_skipped():
    now = datetime.now(UTC).replace(microsecond=0)
    ev = news.parse_faireconomy(feed(now))
    assert [(e["currency"], e["impact"]) for e in ev] == [("USD", "HIGH"), ("EUR", "MEDIUM")]
    assert datetime.fromisoformat(ev[0]["time"]) == now + timedelta(minutes=10)
    assert news.parse_faireconomy({"not": "a list"}) == []


@pytest.fixture
def calendar(monkeypatch, db_clean):
    monkeypatch.setenv("RR_NEWS_PROVIDER", "faireconomy")
    get_settings.cache_clear()
    now = datetime.now(UTC).replace(microsecond=0)
    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["ua"] = url, kw.get("headers", {}).get("User-Agent")
        return httpx.Response(200, json=feed(now), request=httpx.Request("GET", url))
    monkeypatch.setattr(news.httpx, "get", fake_get)
    get_redis().delete("rr:news:last_ok")
    yield seen
    get_settings.cache_clear()


def test_built_in_calendar_makes_news_risk_known(calendar):
    with session_scope() as db:
        assert news.refresh(db) == {"ok": True, "events": 2}
    assert calendar["url"] == news.FAIRECONOMY_URL and calendar["ua"] == "RR-Trading-One/1.0"
    assert news.provider_healthy()
    with session_scope() as db:
        assert {e.provider for e in db.scalars(select(EconomicEvent))} == {"faireconomy"}
        sym = lambda b, q: type("S", (), {"base_currency": b, "profit_currency": q})()  # noqa: E731
        assert news.status_for(db, sym("EUR", "USD"), "EURUSD")[0] == "RESTRICTED", "NFP in 10 minutes: USD pairs"
        assert news.status_for(db, sym("EUR", "JPY"), "EURJPY")[0] == "CLEAR"
        assert news.status_for(db, None, "XAUUSD")[0] == "RESTRICTED", "gold follows USD news"


def test_diagnosis_names_every_closed_gate_with_its_fix(stack):
    fake, bridge = stack
    with session_scope() as db:
        d = execution_doctor.diagnose(db)
    closed = {g["gate"]: g for g in d["gates"] if not g["ok"]}
    assert {"Trading worker running", "Auto trading ON", "Startup checks passed", "News calendar available"} <= set(closed)
    assert d["can_execute"] is False and "Start Trading Worker" in closed["Trading worker running"]["fix"]
    assert "calendar" in closed["News calendar available"]["fix"]
    # open the pipeline: startup checks, auto trading, worker, calendar
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        tr.get_state(db).auto_trading = True
    get_redis().set("rr:role:trading:lock", "test", ex=60)
    get_redis().set("rr:news:last_ok", datetime.now(UTC).isoformat())
    try:
        with session_scope() as db:
            d = execution_doctor.diagnose(db)
    finally:
        get_redis().delete("rr:role:trading:lock")
    assert all(g["ok"] for g in d["gates"] if g["gate"] != "News calendar available") or d["can_execute"]


def test_the_risk_engines_real_rejections_are_counted_with_the_fix(stack):
    fake, bridge = stack
    assert worker.startup_checks(bridge)[0]
    for i in range(2):                                    # news UNKNOWN blocks by default: two refused entries
        with session_scope() as db, pytest.raises(em.ExecutionBlocked):
            open_eur(db, bridge, key=f"k{i}")
    with session_scope() as db:
        day = execution_doctor.last_day(db)
    top = day["risk_rejections"][0]
    assert top["check"] == "news" and top["count"] == 2 and "calendar" in top["fix"]
    assert fake.send_calls == 0, "nothing was relaxed: the refused entries never reached the broker"


def test_chat_trade_is_not_executing_runs_the_diagnosis(stack):
    q = "trade is not executing optimize engine to execute trade smoothly"
    assert agent.workflow_for(q)[0] == "execution" and agent.is_command(q)
    assert not agent.is_command("why was signal #3 not executed?"), "signal questions keep their own answer"
    with session_scope() as db:
        t = agent.start(db, q, None)
        text = agent.describe(t)
    assert "Trades cannot execute" in text and "Start Trading Worker" in text and "Last 24 h" in text
    with session_scope() as db:
        assert tr.get_state(db).auto_trading is False, "the diagnosis changes nothing"
