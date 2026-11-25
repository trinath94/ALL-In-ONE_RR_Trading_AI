"""Error detection and self-healing: classification, de-duplication, re-opening, detectors, auto-resolution, the safe
repair catalogue (verified, recorded, escalated after repeated failure, never touching trading controls), the policy
switch, console errors, unhandled API errors, AI diagnostic assistance and the learning statistics."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models_intel import LearningJob
from app.db.models_opportunity import Incident
from app.db.session import session_scope
from app.services import incidents as inc
from app.services.events import get_redis, system_event
from app.services import trading as tr
from tests.test_ai_gateway import Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)


@pytest.mark.parametrize("component,message,expected", [
    ("worker", "Identity check failed: login 5 does not match", "identity_mismatch"),
    ("execution", "Kill switch: manual", "kill_switch"),
    ("risk", "Risk lock: daily loss limit", "risk_lock"),
    ("execution", "Outcome unknown for abc123; reconciliation required", "execution_unknown"),
    ("connector", "[pc] MT5 terminal disconnected", "mt5_disconnected"),
    ("execution", "Unknown broker symbol FOO.r (run symbol discovery)", "invalid_symbol"),
    ("monitor", "Job backlog", "worker_stalled"),
    ("api", "GET /x: OperationalError: could not connect to server", "database"),
    ("worker", "Unknown engine pending orders at the broker", "unknown_pending_orders"),
    ("api", "GET /x: ZeroDivisionError: division by zero", "application_exception"),
])
def test_classification(component, message, expected):
    assert inc.classify(component, message).key == expected


def test_deduplication_reopening_and_recurrence(db_clean):
    with session_scope() as db:
        a = inc.record(db, "execution", "error", "Outcome unknown for 1a2b3c4d5e; reconciliation required")
        b = inc.record(db, "execution", "error", "Outcome unknown for 9f8e7d6c5b; reconciliation required")
        assert a.id == b.id and b.occurrences == 2, "ids and numbers do not split one problem in two"
        assert inc.record(db, "scanner", "info", "Discovered 12 broker symbols") is None, "info is not an incident"
        inc.resolve(db, b, None, "fixed")
        c = inc.record(db, "execution", "error", "Outcome unknown for 77; reconciliation required")
        assert c.id == a.id and c.status == "OPEN" and c.reopened == 1 and c.attempts == []
        for _ in range(3):
            inc.record(db, "execution", "error", "Outcome unknown for 1; reconciliation required")
        assert "recurring" in c.diagnosis
        assert c.explanation and c.recommended_fix and c.repair == "reconcile_journal"


def test_system_events_become_incidents_and_secrets_are_not_stored(db_clean, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.events.publish", lambda kind, data: seen.append((kind, data)))
    with session_scope() as db:
        system_event(db, "execution", "critical", "Kill switch: test", {"password": "hunter2", "ok": 1})
        system_event(db, "scanner", "info", "routine")
    with session_scope() as db:
        rows = db.scalars(select(Incident)).all()
        assert [r.category for r in rows] == ["kill_switch"] and rows[0].severity == "critical"
        assert "hunter2" not in json.dumps(rows[0].data)
    assert any(k == "incident" and d["event"] == "new" for k, d in seen), "a popup event was published"


def test_kill_switch_incident_closes_only_after_the_administrator_resets_it(db_clean):
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch = True
        system_event(db, "execution", "critical", "Kill switch: test")
    with session_scope() as db:
        i = db.scalar(select(Incident))
        i.last_seen = datetime.now(UTC) - timedelta(minutes=5)
        assert inc.heal(db)["auto_resolved"] == 0 and i.status == "OPEN"
        with pytest.raises(ValueError):
            inc.attempt(db, i)                                      # never repaired automatically
        assert tr.get_state(db).kill_switch, "self-healing never resets the kill switch"
        tr.get_state(db).kill_switch = False                     # the administrator's reset
        assert inc.heal(db)["auto_resolved"] == 1 and i.status == "AUTO_RESOLVED"


def stuck_job(db):
    db.add(LearningJob(job_type="experiments", idempotency_key="stuck-1", status="RUNNING",
                       started_at=datetime.now(UTC) - timedelta(hours=5)))
    db.flush()


def test_stuck_job_is_detected_repaired_verified_and_recorded(db_clean):
    with session_scope() as db:
        stuck_job(db)
        found = inc.run_detectors(db)
        i = next(x for x in found if x.category == "worker_stalled")
        out = inc.heal(db)
        assert out["repairs"] >= 1
        job = db.scalar(select(LearningJob).where(LearningJob.idempotency_key == "stuck-1"))
        assert job.status == "INTERRUPTED"
        assert i.status == "AUTO_RESOLVED" and i.attempts[-1]["verified"] is True and i.attempts[-1]["by"] == "auto"


def test_the_policy_switch_stops_automatic_repairs(db_clean):
    from app.strategies.profiles import policy
    with session_scope() as db:
        policy(db).auto_repair = False
        stuck_job(db)
        i = next(x for x in inc.run_detectors(db) if x.category == "worker_stalled")
        assert inc.heal(db)["repairs"] == 0 and i.attempts == [] and i.status == "OPEN"
        assert db.scalar(select(LearningJob.status).where(LearningJob.idempotency_key == "stuck-1")) == "RUNNING"


def test_failed_repairs_escalate_after_three_attempts(db_clean, monkeypatch):
    notes = []
    monkeypatch.setattr("app.notifications.service.notify", lambda *a, **k: notes.append(a))
    monkeypatch.setitem(inc.REPAIRS, "refresh_symbols", (lambda db, ctx: (False, "boom"), "test repair"))
    with session_scope() as db:
        i = inc.record(db, "execution", "warning", "Unknown broker symbol X.r")
        for _ in range(3):
            inc.attempt(db, i)
        assert i.severity == "critical" and len(i.attempts) == 3 and i.status == "OPEN" and notes
        assert inc.heal(db)["repairs"] == 0, "no fourth automatic attempt"


def test_mt5_disconnection_detected_and_closed_when_the_connection_returns(db_clean):
    r = get_redis()
    r.set("rr:bridge:health", json.dumps({"connected": False, "error": "terminal closed"}), ex=60)
    with session_scope() as db:
        i = next(x for x in inc.run_detectors(db) if x.category == "mt5_disconnected")
        i.last_seen = datetime.now(UTC) - timedelta(minutes=2)
    r.set("rr:bridge:health", json.dumps({"connected": True}), ex=60)
    with session_scope() as db:
        inc.heal(db)
        assert db.get(Incident, i.id).status == "AUTO_RESOLVED"


def test_disk_and_resource_alerts_become_incidents(db_clean, monkeypatch):
    from app.services import resources
    monkeypatch.setattr(resources, "overview", lambda db, r=None: {
        "disk": {"free_gb": 1.0}, "alerts": [{"kind": "DISK_SPACE", "severity": "critical", "detail": "1.0 GB free"},
                                            {"kind": "RESOURCES_ANALYSIS", "severity": "warn", "detail": "memory at 91%"}]})
    with session_scope() as db:
        cats = {x.category: x for x in inc.run_detectors(db)}
        assert cats["disk_space"].severity == "critical" and cats["disk_space"].repair == "prune_history"
        assert "resources_high" in cats


def test_incident_api_permissions_and_actions(client):
    with session_scope() as db:
        i = inc.record(db, "execution", "warning", "Unknown broker symbol X.r")
        k = inc.record(db, "execution", "critical", "Kill switch: test")
        iid, kid = i.id, k.id
    login(client, "viewer")
    rows = client.get("/api/incidents?status=open").json()
    assert {r["id"] for r in rows} == {iid, kid}
    assert client.post(f"/api/incidents/{iid}/ack", headers=h(client)).status_code == 403
    s = client.get("/api/incidents/summary").json()
    assert s["open_by_severity"] == {"warning": 1, "critical": 1} and s["auto_repair"] is True
    assert "kill_switch" in s["never_automated"]
    login(client, "trader")
    assert client.post(f"/api/incidents/{iid}/ack", headers=h(client)).json()["status"] == "ACKNOWLEDGED"
    assert client.post(f"/api/incidents/{iid}/resolve", json={}, headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post(f"/api/incidents/{kid}/repair", headers=h(client)).status_code == 409
    r = client.post(f"/api/incidents/{iid}/repair", headers=h(client)).json()
    assert r["attempt"]["action"] == "refresh_symbols" and r["status"] == "RESOLVED"
    assert client.post(f"/api/incidents/{kid}/resolve", json={"resolution": "checked"},
                       headers=h(client)).json()["status"] == "RESOLVED"
    assert client.put("/api/automation/policy", json={"auto_repair": False}, headers=h(client)).json()["auto_repair"] is False


def test_console_errors_and_unhandled_api_errors_become_incidents(client, monkeypatch):
    login(client, "viewer")
    r = client.post("/api/client-errors", json={"message": "TypeError: x is undefined", "page": "/signals?id=4",
                                                "stack": "at Foo"}, headers=h(client))
    assert r.status_code == 200 and r.json()["incident_id"]
    from fastapi.testclient import TestClient
    from app.main import app
    import app.api.routes.system as system_routes
    monkeypatch.setattr(system_routes, "_incident_out", lambda i: 1 / 0)
    c = TestClient(app, raise_server_exceptions=False)
    login(c, "admin")
    assert c.get("/api/incidents").status_code == 500
    with session_scope() as db:
        cats = {x.category: x for x in db.scalars(select(Incident))}
    assert cats["frontend_error"].data["page"] == "/signals"
    assert "ZeroDivisionError" in cats["application_exception"].message and cats["application_exception"].component == "api"


def test_ai_diagnosis_is_advice_only(client, keys):
    a = provider("diag")
    route("diagnostics", (a, "m"))
    with session_scope() as db:
        iid = inc.record(db, "connector", "error", "[pc] MT5 terminal disconnected").id
    answer = {"explanation": "terminal closed", "likely_causes": ["PC asleep"], "recommended_steps": ["open MT5"]}
    import app.ai.gateway as gw
    real = gw.run
    gw_calls = []

    def run(task, *a, **k):
        gw_calls.append(task)
        return real(task, *a, **{**k, "provider_factory": Scripted({"diag": [answer]}).factory})
    import unittest.mock as um
    with um.patch.object(gw, "run", run):
        login(client, "admin")
        r = client.post(f"/api/incidents/{iid}/diagnose", headers=h(client)).json()
    assert gw_calls == ["diagnostics"] and r["ai_diagnosis"]["likely_causes"] == ["PC asleep"]
    assert "not executed" in r["ai_diagnosis"]["note"] and r["status"] == "OPEN"


def test_learning_statistics(db_clean):
    with session_scope() as db:
        stuck_job(db)
        inc.run_detectors(db)
        inc.heal(db)
        inc.record(db, "execution", "critical", "Kill switch: x")
        s = inc.stats(db, 30)
    cats = {c["category"]: c for c in s["categories"]}
    assert cats["worker_stalled"]["auto_resolved"] == 1 and cats["worker_stalled"]["self_healed_share"] == 1.0
    assert cats["kill_switch"]["open"] == 1 and s["open"] >= 1
