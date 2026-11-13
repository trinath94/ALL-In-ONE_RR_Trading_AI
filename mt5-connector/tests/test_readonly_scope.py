"""Read-only bridge credential: reads are allowed, execution is refused before anything reaches MT5. Without a
read-only secret configured, the connector behaves exactly as before."""
import pytest
from fastapi.testclient import TestClient

from connector.adapter import MT5Adapter
from connector.app import ConnectorConfig, create_app, read_only_allowed
from fake_mt5 import FakeMT5
from test_connector import SECRET, call, order

READ = "r" * 40


@pytest.fixture
def scoped(tmp_path):
    fake = FakeMT5()
    cfg = ConnectorConfig(SECRET, 1001, "Demo-Server", "pw", journal_path=str(tmp_path / "j.db"), readonly_secret=READ)
    return fake, TestClient(create_app(cfg, MT5Adapter(fake), connect=False))


def test_read_secret_can_read(scoped):
    _, c = scoped
    for path in ("/health", "/account", "/positions", "/orders", "/symbols", "/executions/does-not-exist"):
        assert call(c, "GET", path, secret=READ).status_code == 200, path
    r = call(c, "POST", "/calc/margin", {"action": "BUY", "symbol": "EURUSD.r", "volume": 0.1, "price": 1.1}, secret=READ)
    assert r.status_code == 200


def test_read_secret_cannot_execute(scoped):
    fake, c = scoped
    r = call(c, "POST", "/executions", order("ro-1"), secret=READ)
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert fake.send_calls == 0 and not fake.positions, "a read-only request must never reach MT5"
    # the journal never saw it, so the same id with the trading credential is a fresh, normal execution
    r2 = call(c, "POST", "/executions", order("ro-1"))
    assert r2.status_code == 200 and r2.json()["state"] == "DONE"
    assert fake.send_calls == 1 and len(fake.positions) == 1


def test_refused_read_request_does_not_burn_a_nonce_for_the_full_secret(scoped):
    _, c = scoped
    assert call(c, "POST", "/executions", order("ro-2"), secret=READ).status_code == 403
    assert call(c, "GET", "/health").status_code == 200


def test_unknown_secret_still_rejected(scoped):
    _, c = scoped
    assert call(c, "GET", "/health", secret="x" * 40).status_code == 401


def test_without_readonly_secret_behaviour_is_unchanged(tmp_path):
    fake = FakeMT5()
    cfg = ConnectorConfig(SECRET, 1001, "Demo-Server", "pw", journal_path=str(tmp_path / "j.db"))
    c = TestClient(create_app(cfg, MT5Adapter(fake), connect=False))
    assert call(c, "GET", "/health", secret=READ).status_code == 401
    assert call(c, "POST", "/executions", order("full-1")).json()["state"] == "DONE"


def test_readonly_secret_must_be_strong_and_distinct(tmp_path):
    for bad in ("short", SECRET):
        with pytest.raises(RuntimeError):
            create_app(ConnectorConfig(SECRET, 1001, "S", journal_path=str(tmp_path / "j.db"), readonly_secret=bad),
                       MT5Adapter(FakeMT5()), connect=False)


def test_read_only_allowlist():
    assert read_only_allowed("GET", "/anything")
    assert read_only_allowed("POST", "/calc/margin")
    assert read_only_allowed("POST", "/calc/margin?x=1")
    assert not read_only_allowed("POST", "/executions")
    assert not read_only_allowed("DELETE", "/positions/1")
    assert not read_only_allowed("PUT", "/calc/margin")
