"""Parts 27-30 Phase 9: the DEMO validation report reads only recorded evidence, marks missing activity PENDING (never
a pass), and flags problems. Simulated broker."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import OpenPosition, TradingAccount
from app.db.session import session_scope
from app.scanner.service import scan_once
from app.services import demo_validation as dv
from app.workers import main as worker
from tests.test_api import client, login, make_user  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import scan  # noqa: F401 (fixture)
from tests.test_part29 import seed_demo_trades


def items():
    with session_scope() as db:
        r = dv.report(db)
    return r, {i["key"]: i for i in r["items"]}


def test_fresh_install_is_pending_not_passed(db_clean):
    r, it = items()
    assert it["mode"]["status"] == "PASS"
    assert it["scanning"]["status"] in ("PENDING", "ATTENTION") and it["opportunities"]["status"] == "PENDING"
    assert it["execution"]["status"] == "PENDING" and it["demo_record"]["status"] == "PENDING"
    assert r["accepted"] is False, "nothing recorded yet: never accepted"


def test_activity_turns_items_into_evidence(scan):
    fake, bridge, _ = scan
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:                                       # before the scan records the test feed's anomaly
        open_eur(db, bridge, key="dv1")
    scan_once(bridge, None, lambda _id: None)
    seed_demo_trades(25, profit=-1.0)
    r, it = items()
    assert it["scanning"]["status"] == "PASS" and it["data_quality"]["status"] in ("PASS", "ATTENTION")
    assert it["opportunities"]["status"] == "PASS"
    assert it["ai_review"]["status"] == "PENDING" and "never auto-executed" in it["ai_review"]["detail"]
    assert it["demo_record"]["status"] == "ATTENTION" and "net negative" in it["demo_record"]["detail"]
    assert it["execution"]["status"] in ("PENDING", "PASS"), "a manual order has no signal: not counted as auto execution"
    with session_scope() as db:
        from app.services import trading as tr
        tr.sync_positions(db, bridge, tr.active_account(db))
        db.scalar(select(OpenPosition)).stop_loss = None
    _, it = items()
    assert it["protection"]["status"] == "ATTENTION"


def test_api_needs_a_session(client):
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/validation/demo").status_code == 401
        make_user("viewer31", "readonly")
        login(c, "viewer31")
        assert c.get("/api/validation/demo").json()["items"]
