"""Engine Assistant: grounded, read-only explanations of the engine and of where a signal stopped, with the next step;
the AI only phrases the engine's facts and the engine answers by itself without AI."""
from sqlalchemy import select

from app.db.models import TradeOrder, TradingAccount, TradingSignal
from app.db.session import session_scope
from app.scanner.service import scan_once
from app.services import assistant
from app.services import trading as tr
from app.services.events import get_redis
from app.workers import main as worker
from tests.test_accounts_execution import ready, two  # noqa: F401 (fixture)
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto


def worker_on():
    get_redis().set("rr:role:trading:lock", "1", ex=60)


def snapshot():
    with session_scope() as db:
        st = tr.get_state(db)
        return (st.auto_trading, st.kill_switch, st.ai_mode, st.active_mode,
                db.query(TradeOrder).count(), db.query(TradingSignal).count())


def test_next_steps_name_what_blocks_trading(stack):
    with session_scope() as db:
        steps = assistant.next_steps(assistant.overview(db))
    text = " ".join(steps)
    assert "Start Trading Worker" in text and "Start auto" in text
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch, st.kill_reason = True, "manual test"
        steps = assistant.next_steps(assistant.overview(db))
    assert steps[0].startswith("Kill switch is ON (manual test)")


def scanned(scan, monkeypatch, mode="ENGINE_ONLY", auto=True):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    if auto:
        enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = mode
    worker_on()
    scan_once(bridge, None, worker.auto_execute_factory(bridge))
    from datetime import UTC, datetime
    with session_scope() as db:
        done = db.scalar(select(TradeOrder.signal_id).where(TradeOrder.status == "CONFIRMED").limit(1))
        return done or db.scalar(select(TradingSignal.id).where(TradingSignal.decision == "BUY",
                                                                TradingSignal.expires_at > datetime.now(UTC))
                                 .order_by(TradingSignal.id.desc()))


def test_an_executed_signal_is_reported_executed(scan, monkeypatch):
    sid = scanned(scan, monkeypatch)
    with session_scope() as db:
        d = assistant.diagnose_signal(db, db.get(TradingSignal, sid))
    assert d["verdict"].startswith("executed on 1 account") and d["next_step"] is None
    assert d["accounts"][0]["order"]["status"] == "CONFIRMED" and d["accounts"][0]["risk_engine"] == "APPROVED"


def test_auto_trading_off_is_where_the_signal_stopped(scan, monkeypatch):
    sid = scanned(scan, monkeypatch, auto=False)
    with session_scope() as db:
        d = assistant.diagnose_signal(db, db.get(TradingSignal, sid))
    assert d["verdict"] == "not executed: stopped at 'Auto trading'" and "Start auto" in d["next_step"]


def test_ai_mode_without_a_provider_points_to_ai_studio(scan, monkeypatch, keys):
    sid = scanned(scan, monkeypatch, mode="AI")
    with session_scope() as db:
        d = assistant.diagnose_signal(db, db.get(TradingSignal, sid))
    ai = next(c for c in d["checks"] if c["check"] == "ai")
    assert ai["ok"] is False and "AI Studio" in ai["next_step"] and "Without AI" in ai["next_step"]
    assert d["verdict"] == "not executed: stopped at 'AI review'"


def test_a_risk_engine_refusal_is_shown_per_account(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    ready(fake1, fake2, bridge1, bridges, shift, monkeypatch)
    worker_on()
    with session_scope() as db:
        db.get(TradingAccount, a2).risk_overrides = {"max_spread_points": 0.1}
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    with session_scope() as db:
        sid = db.scalar(select(TradeOrder.signal_id).where(TradeOrder.status == "CONFIRMED").limit(1))
        d = assistant.diagnose_signal(db, db.get(TradingSignal, sid))
    rows = {r["account"]: r for r in d["accounts"]}
    assert rows["DEMO Account 2"]["risk_engine"] == "REJECTED" and rows["DEMO Account 2"]["risk_reasons"]
    assert "Risk Engine refused" in rows["DEMO Account 2"]["next_step"]
    assert d["verdict"].startswith("executed on 1 account")


def test_questions_are_routed_and_answered_without_ai(scan, monkeypatch):
    sid = scanned(scan, monkeypatch, auto=False)
    before = snapshot()
    with session_scope() as db:
        a = assistant.answer(db, f"why was signal #{sid} not executed?")
        assert a["intent"] == "signal" and a["source"] == "engine" and f"Signal #{sid}" in a["answer"]
        assert "Next step:" in a["answer"]
        b = assistant.answer(db, "why is it not trading?")
        assert b["intent"] == "why" and b["diagnosis"]["signal"]["decision"] == "BUY"
        c = assistant.answer(db, "what should I do next?")
        assert c["intent"] == "next" and "What to do next:" in c["answer"]
        d = assistant.answer(db, "engine status")
        assert d["intent"] == "status" and "Mode DEMO" in d["answer"]
        assert assistant.answer(db, "signal #999999")["answer"] == "There is no signal #999999."
    assert snapshot() == before, "the assistant never changes anything"


def test_the_ai_phrases_the_engine_facts_and_falls_back(stack, keys):
    a = provider("chat")
    route("engine_assistant", (a, "m"))
    seen = {}

    class Spy(Scripted):
        def factory(self, h):
            inner = super().factory(h)

            class P:
                def complete_json(_, system, payload, schema, model, max_tokens):
                    seen["payload"], seen["system"] = payload, system
                    return inner.complete_json(system, payload, schema, model, max_tokens)
            return P()
    with session_scope() as db:
        out = assistant.answer(db, "status, my password is hunter2",
                               provider_factory=Spy({"chat": [{"answer": "All quiet.", "next_steps": ["Start auto"]}]}).factory)
    assert out["source"] == "ai" and out["answer"] == "All quiet." and out["engine_answer"]
    assert "overview" in seen["payload"] and "never claim to have changed anything" in seen["system"]
    with session_scope() as db:
        out = assistant.answer(db, "status", provider_factory=Scripted({"chat": [{"bad": 1}] * 5}).factory)
    assert out["source"] == "engine" and out["ai_error"]


def test_api(client, stack):
    login(client, "viewer")
    r = client.post("/api/assistant/ask", json={"question": "what should I do next?"}, headers=h(client))
    assert r.status_code == 200 and r.json()["next_steps"]
    assert client.get("/api/assistant/overview").json()["overview"]["mode"] == "DEMO"
    assert client.get("/api/assistant/signals/999999").status_code == 404
    assert client.post("/api/assistant/ask", json={"question": ""}, headers=h(client)).status_code == 422
