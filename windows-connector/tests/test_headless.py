"""Headless connector (RR Trading One): explicit profile, isolated instance, status/stop files, single instance."""
import io
import json
import threading
import time

import pytest

from rr_connector import headless
from rr_connector.terminals import Terminal

EXE = "C:/MT5/terminal64.exe"
LOOPBACK = "http://127.0.0.1:47810"


@pytest.fixture
def kr(monkeypatch, home):
    """One in-memory keyring shared by every Vault the headless code creates."""
    from rr_connector import vault as vmod
    ring = vmod.MemoryKeyring()
    monkeypatch.setattr(vmod, "_default_backend", lambda: ring)
    return ring


def profile_file(tmp_path, **over):
    d = {"profile_id": "demo-1", "name": "Demo", "terminal_exe": EXE, "server": "Demo-Server", "port": 443,
         "expected_login": 1001, "engine_url": LOOPBACK, **over}
    p = tmp_path / f"profile-{abs(hash(json.dumps(d, sort_keys=True)))}.json"
    p.write_text(json.dumps(d))
    return str(p)


def registered(kr):
    store, vault = headless._open_store()
    store.update(agent_id=7, engine_url=LOOPBACK)
    vault.set("bridge-secret", "b" * 48)
    vault.set("bridge-readonly-secret", "r" * 48)
    vault.set("api-key", "k" * 40)
    store.close()


def status(home):
    p = home / headless.STATUS_FILE
    return json.loads(p.read_text()) if p.exists() else {}


def wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def run_in_thread(profile, fake, kr, poll_s=0.1):
    from connector.adapter import MT5Adapter
    term = Terminal(folder="C:/MT5", exe=EXE, running=True, sources=["process"])
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("rc", headless.run(profile, MT5Adapter(fake), lambda: [term],
                                                                          poll_s=poll_s)), daemon=True)
    t.start()
    return t, box


def test_profile_validation(tmp_path):
    ok = headless.Profile.load(profile_file(tmp_path))
    assert ok.expected_login == 1001 and ok.terminal_exe == EXE
    for bad in ({"terminal_exe": "C:/evil/cmd.exe"}, {"expected_login": 0}, {"server": " "},
                {"engine_url": "http://engine.example.com"}, {"engine_url": "file:///etc/passwd"}):
        with pytest.raises(ValueError):
            headless.Profile.load(profile_file(tmp_path, **bad))
    assert headless.Profile.load(profile_file(tmp_path, engine_url="https://engine.example.com")).engine_url


def test_mask_login():
    assert headless.mask_login(51234567) == "•••••567"
    assert headless.mask_login(None) == "" and headless.mask_login(12) == "12"


def test_status_document_masks_login_and_contains_no_secrets(kr, home):
    registered(kr)
    store, vault = headless._open_store()
    snap = {"account": {"login": 51234567, "server": "Demo-Server", "company": "Broker Ltd", "trade_mode": 0,
                        "currency": "USD", "balance": 1000.0, "equity": 990.0, "margin_free": 950.0, "leverage": 100},
            "terminal": {"connected": True, "trade_allowed": False, "build": 4000, "ping_last": 25000},
            "engine": "CONNECTED", "engine_rtt_ms": 4.2, "mt5": "CONNECTED"}
    doc = headless.status_document(snap, None, "CONNECTED", "", store)
    text = json.dumps(doc)
    assert "51234567" not in text and doc["account"]["login"] == "•••••567"
    assert doc["account"]["mode"] == "DEMO" and doc["terminal"]["algo_trading"] is False
    assert doc["terminal"]["ping_ms"] == 25.0 and doc["engine"]["latency_ms"] == 4.2
    for secret in (vault.get("bridge-secret"), vault.get("bridge-readonly-secret"), vault.get("api-key")):
        assert secret not in text
    store.close()


def test_not_registered_exits_with_clear_status(kr, home, tmp_path):
    assert headless.run(profile_file(tmp_path)) == headless.EXIT_NOT_REGISTERED
    assert status(home)["state"] == "NOT_REGISTERED"


def test_registration_for_another_engine_is_not_used(kr, home, tmp_path):
    registered(kr)
    store, _ = headless._open_store()
    store.set("engine_url", "https://old-ubuntu.example.com")   # linked to a different engine
    store.close()
    assert headless.run(profile_file(tmp_path)) == headless.EXIT_NOT_REGISTERED


def test_invalid_profile_reported(kr, home, tmp_path):
    assert headless.run(profile_file(tmp_path, terminal_exe="C:/x/notepad.exe")) == headless.EXIT_USAGE
    assert status(home)["state"] == "INVALID_PROFILE"


def test_connects_to_profile_terminal_and_stops_gracefully(kr, home, tmp_path):
    from fake_mt5 import FakeMT5
    registered(kr)
    fake = FakeMT5(login=1001, server="Demo-Server")
    t, box = run_in_thread(profile_file(tmp_path), fake, kr)
    assert wait(lambda: status(home).get("state") == "CONNECTED"), status(home)
    st = status(home)
    assert st["account"]["login"] == "•001"
    assert st["account"]["mode"] == "DEMO" and st["terminal"]["path"] == EXE and st["profile"]["id"] == "demo-1"
    assert st["pid"] and st["updated_at"] > time.time() - 5
    (home / headless.STOP_FILE).touch()                         # graceful stop request from the desktop
    t.join(timeout=15)
    assert not t.is_alive() and box["rc"] == headless.EXIT_OK
    assert status(home)["state"] == "STOPPED" and not (home / headless.STOP_FILE).exists()


def test_first_status_after_connect_is_not_stale(kr, home, tmp_path):
    """Regression: the first status written must reflect the connect that just happened (was DISCONNECTED)."""
    from fake_mt5 import FakeMT5
    registered(kr)
    t0 = time.time()
    t, box = run_in_thread(profile_file(tmp_path), FakeMT5(login=1001, server="Demo-Server"), kr, poll_s=3)
    assert wait(lambda: status(home).get("state") not in (None, ""), timeout=2.5)
    assert time.time() - t0 < 3, "must be the first write (the next one comes 3 s later)"
    assert status(home)["state"] == "CONNECTED", status(home)
    (home / headless.STOP_FILE).touch()
    t.join(timeout=10)
    assert box["rc"] == headless.EXIT_OK


def test_wrong_account_is_disconnected_not_traded(kr, home, tmp_path):
    from fake_mt5 import FakeMT5
    registered(kr)
    fake = FakeMT5(login=2002, server="Demo-Server")             # terminal logged in to a different account
    t, box = run_in_thread(profile_file(tmp_path), fake, kr)
    assert wait(lambda: status(home).get("state") == "PROFILE_MISMATCH"), status(home)
    detail = status(home)["detail"]
    assert "•002" in detail and "•001" in detail and "2002" not in detail   # masked even in the message
    assert status(home)["account"]["login"] == ""                 # disconnected: no account shown as attached
    (home / headless.STOP_FILE).touch()
    t.join(timeout=15)
    assert box["rc"] == headless.EXIT_OK


def test_single_instance(kr, home):
    lock = headless.InstanceLock(home / headless.LOCK_FILE)
    try:
        assert headless.run("unused.json") == headless.EXIT_ALREADY_RUNNING
    finally:
        lock.f.close()


def test_register_stdin_reads_token_from_stdin_and_reports_failure(kr, home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("rrmt5_" + "t" * 40 + "\n"))
    rc = headless.register_stdin("http://127.0.0.1:9")            # nothing listens there
    out = json.loads(capsys.readouterr().out)
    assert rc == headless.EXIT_ERROR and out["ok"] is False and "rrmt5_" not in json.dumps(out)


def test_vault_is_isolated_from_the_standalone_connector(kr):
    from rr_connector.vault import SERVICE, Vault
    standalone, mine = Vault(kr), Vault(kr, service=headless.VAULT_SERVICE)
    standalone.set("bridge-secret", "remote-ubuntu-secret")
    mine.set("bridge-secret", "local-secret")
    assert standalone.get("bridge-secret") == "remote-ubuntu-secret" and SERVICE != headless.VAULT_SERVICE
