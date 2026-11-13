import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from connector.adapter import MT5Adapter  # noqa: E402
from connector.app import ConnectorConfig, create_app  # noqa: E402
from connector.journal import Journal  # noqa: E402
from connector.service import tag  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402

SECRET = "s" * 40
sys.path.insert(0, os.path.join(ROOT, "..", "backend"))
from app.core.security import sign_request  # noqa: E402  (same signer used by the Linux side)


@pytest.fixture
def env(tmp_path):
    fake = FakeMT5()
    cfg = ConnectorConfig(SECRET, 1001, "Demo-Server", "pw", journal_path=str(tmp_path / "j.db"))
    app = create_app(cfg, MT5Adapter(fake), connect=False)
    return fake, TestClient(app), app


def call(client, method, path, payload=None, secret=SECRET, headers=None):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() if payload is not None else b""
    h = {"Content-Type": "application/json", **sign_request(secret, method, path, body), **(headers or {})}
    return client.request(method, path, content=body or None, headers=h)


def order(eid="exec-1", **kw):
    return {"execution_id": eid, "action": "MARKET_BUY", "symbol": "EURUSD.r", "volume": 0.1, "stop_loss": 1.095,
            "take_profit": 1.11, "account_login": 1001, "account_server": "Demo-Server", **kw}


def test_rejects_unsigned_and_bad_signature(env):
    _, c, _ = env
    assert c.get("/health").status_code == 401
    assert call(c, "GET", "/health", secret="x" * 40).status_code == 401
    assert call(c, "GET", "/health").status_code == 200


def test_replayed_nonce_rejected(env):
    _, c, _ = env
    h = sign_request(SECRET, "GET", "/health", b"")
    assert c.get("/health", headers=h).status_code == 200
    r = c.get("/health", headers=h)
    assert r.status_code == 401 and "replay" in r.json()["detail"]


def test_market_order_executes_once(env):
    fake, c, _ = env
    r1 = call(c, "POST", "/executions", order()).json()
    assert r1["state"] == "DONE" and r1["retcode"] == 10009
    r2 = call(c, "POST", "/executions", order()).json()
    assert r2["replayed"] is True and r2["state"] == "DONE"
    assert fake.send_calls == 1, "duplicate execution_id must not reach MT5 twice"
    assert len(fake.positions) == 1
    pos = next(iter(fake.positions.values()))
    assert pos["comment"] == tag("exec-1") and pos["sl"] == 1.095


def test_account_identity_mismatch_rejected(env):
    fake, c, _ = env
    r = call(c, "POST", "/executions", order(account_login=999)).json()
    assert r["state"] == "REJECTED" and "identity" in r["reason"]
    assert fake.send_calls == 0


def test_crash_after_send_reconciles_without_resend(env):
    fake, c, _ = env
    fake.fail_after_send = True
    r = call(c, "POST", "/executions", order("exec-crash")).json()
    # order reached the broker; reconciliation finds the tagged deal
    assert r["state"] == "DONE" and r.get("reconciled") is True
    fake.fail_after_send = False
    again = call(c, "POST", "/executions", order("exec-crash")).json()
    assert again["state"] == "DONE"
    assert fake.send_calls == 1


def test_broker_rejection_is_not_success(env):
    fake, c, _ = env
    fake.reject_retcode = 10019
    r = call(c, "POST", "/executions", order("exec-nomoney")).json()
    assert r["state"] == "REJECTED" and r["retcode"] == 10019 and r["retcode_text"] == "Not enough money"


def test_execution_lookup_and_not_found(env):
    _, c, _ = env
    assert call(c, "GET", "/executions/never").json()["state"] == "NOT_FOUND"
    call(c, "POST", "/executions", order("exec-q"))
    assert call(c, "GET", "/executions/exec-q").json()["state"] == "DONE"


def test_modify_partial_and_close(env):
    fake, c, _ = env
    call(c, "POST", "/executions", order("o1", volume=0.2))
    ticket = next(iter(fake.positions))
    base = {"account_login": 1001, "account_server": "Demo-Server", "position_ticket": ticket}
    assert call(c, "POST", "/executions", {"execution_id": "m1", "action": "MODIFY_SLTP", "stop_loss": 1.1,
                                           **base}).json()["state"] == "DONE"
    assert fake.positions[ticket]["sl"] == 1.1
    assert call(c, "POST", "/executions", {"execution_id": "p1", "action": "PARTIAL_CLOSE", "volume": 0.1,
                                           **base}).json()["state"] == "DONE"
    assert fake.positions[ticket]["volume"] == 0.1
    assert call(c, "POST", "/executions", {"execution_id": "c1", "action": "CLOSE", **base}).json()["state"] == "DONE"
    assert ticket not in fake.positions


def test_journal_survives_restart(tmp_path):
    fake = FakeMT5()
    path = str(tmp_path / "j.db")
    cfg = ConnectorConfig(SECRET, 1001, "Demo-Server", journal_path=path)
    c1 = TestClient(create_app(cfg, MT5Adapter(fake), connect=False))
    call(c1, "POST", "/executions", order("persist"))
    # new process, same journal file
    c2 = TestClient(create_app(cfg, MT5Adapter(fake), Journal(path), connect=False))
    r = call(c2, "POST", "/executions", order("persist")).json()
    assert r["replayed"] and r["state"] == "DONE" and fake.send_calls == 1


def test_short_secret_refused(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(ConnectorConfig("short", 1, "x", journal_path=str(tmp_path / "j")), MT5Adapter(FakeMT5()), connect=False)
    _ = time
