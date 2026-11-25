"""Master upgrade: integration. One flow through the real code with only the brokers (two FakeMT5 accounts behind two
real connector apps) and the AI provider (scripted) simulated:

    Single Active AI reviews the candidates -> a validated configuration profile is applied and recorded on the
    signal -> each account trades the signal through its own connector with its own risk limits -> positions are
    closed and synced from each account's deal history -> the learner reviews every closed trade -> an MT5 outage
    becomes an incident that closes itself when the connection returns -> the kill switch applies to both accounts and
    its incident closes only after an administrator resets it -> still DEMO, LIVE never touched."""
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.ai import gateway
from app.db.models import ClosedPosition, TradeOrder, TradingAccount, TradingSignal
from app.db.models_ai import AIControl
from app.db.models_intel import FailureReport
from app.db.models_opportunity import Incident
from app.db.session import session_scope
from app.execution import manager as em
from app.learning_engine import collector, failure, features
from app.scanner.service import scan_once
from app.services import incidents
from app.services import trading as tr
from app.services.events import get_redis
from app.strategies import profiles, registry
from app.workers import main as worker
from tests.test_accounts_execution import ready, two  # noqa: F401 (fixture)
from tests.test_ai_gateway import REVIEW_OK, Scripted, keys, provider  # noqa: F401 (fixture)
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)


def test_master_upgrade_flow_end_to_end_in_demo(two, keys, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    ready(fake1, fake2, bridge1, bridges, shift, monkeypatch)
    with session_scope() as db:
        tr.get_state(db).ai_mode = "AI"                              # with AI: the review must confirm
        db.get(TradingAccount, a2).risk_overrides = {"max_lot_size": 0.01}
        # 1. Single Active AI: one provider for every task, no fallback
        pid = provider("active-ai")
        db.merge(AIControl(id=1, single_active=True, active_provider_id=pid, active_model="m", fallback_enabled=False))
        # 2. a validated, DEMO-active configuration profile per strategy (same values as the defaults)
        made = {}
        for code, cls in registry.all_strategies().items():
            if dict(cls.params):
                k, v = next(iter(dict(cls.params).items()))
                p = profiles.create(db, code, {k: v})
                p.validation, p.status = {"passed": True}, "DEMO_ACTIVE"
                made[code] = f"{p.id}:v{p.version}"
    assert [h.name for h in gateway.chain_for("candidate_review")] == ["active-ai"]
    reviewer = gateway.GatewayReviewer(Scripted({"active-ai": [dict(REVIEW_OK)] * 50}).factory)
    scan_once(bridge1, reviewer, worker.auto_execute_factory(bridge1, bridges))

    # 3. both accounts traded, each within its own limits, each signal once per account
    with session_scope() as db:
        orders = db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED", TradeOrder.source == "AUTO")).all()
        assert {o.account_id for o in orders} == {a1, a2}
        assert all(float(o.volume) <= 0.01 for o in orders if o.account_id == a2)
        for o in orders:
            sig = db.get(TradingSignal, o.signal_id)
            assert sig.ai_review["decision"] == "BUY" and sig.ai_review["model_versions"] is not None
            tagged = [e for e in sig.strategies if e.get("config_profile")]
            assert tagged and all(e["config_profile"] == made[e["strategy"]] for e in tagged)
        assert tr.get_state(db).active_mode == "DEMO"

    # 4. close every position through each account's connector; sync each account's history; review every trade
    for acct_id, fake, b in ((a1, fake1, bridge1), (a2, fake2, bridge2)):
        with session_scope() as db:
            acct = db.get(TradingAccount, acct_id)
            for t in list(fake.positions):
                em.manage(db, b, action="CLOSE", position_ticket=t, dedupe_key=f"it-close-{acct_id}-{t}", account=acct)
            collector.sync_account_history(db, b, acct)
    with session_scope() as db:
        closed = db.scalars(select(ClosedPosition)).all()
        assert {c.account_id for c in closed} == {a1, a2}
        features.enrich_pending(db, None)
        failure.run(db, "DEMO")
    with session_scope() as db:
        reports = db.scalars(select(FailureReport)).all()
        assert len(reports) == len(closed) and all(r.review and r.review["verdict"] for r in reports)

    # 5. an MT5 outage becomes one incident and closes itself when the connection returns
    r = get_redis()
    r.set("rr:bridge:health", json.dumps({"connected": False, "error": "terminal closed"}), ex=60)
    with session_scope() as db:
        incidents.run_detectors(db)
        incidents.run_detectors(db)
        mt5 = db.scalar(select(Incident).where(Incident.category == "mt5_disconnected"))
        assert mt5.occurrences == 2 and mt5.status == "OPEN"
        mt5.last_seen = datetime.now(UTC) - timedelta(minutes=2)
    r.set("rr:bridge:health", json.dumps({"connected": True}), ex=60)
    with session_scope() as db:
        incidents.heal(db, {"bridge": bridge1})
        assert db.scalar(select(Incident.status).where(Incident.category == "mt5_disconnected")) == "AUTO_RESOLVED"

    # 6. the kill switch reaches both accounts; self-healing never resets it
    with session_scope() as db:
        em.kill_switch(db, bridge1, None, "integration test")
    worker.other_accounts_cycle(bridges)
    with session_scope() as db:
        assert db.get(TradingAccount, a2).emergency_applied
        kill = db.scalar(select(Incident).where(Incident.category == "kill_switch"))
        kill.last_seen = datetime.now(UTC) - timedelta(minutes=2)
        incidents.heal(db, {"bridge": bridge1})
        assert kill.status == "OPEN" and tr.get_state(db).kill_switch
        st = tr.get_state(db)
        st.kill_switch, st.kill_reason = False, ""                    # the administrator's reset
        incidents.heal(db, {"bridge": bridge1})
        assert kill.status == "AUTO_RESOLVED"
        assert st.active_mode == "DEMO" and not db.scalars(select(TradingAccount).where(TradingAccount.mode == "LIVE")).all()
