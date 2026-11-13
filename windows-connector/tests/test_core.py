"""Unit tests: terminal detection, connect/validation, guard, encrypted store, updater, device identity."""
import hashlib
import sqlite3

import httpx
import pytest

from connector.adapter import MT5Adapter
from fake_mt5 import FakeMT5
from rr_connector import guard as g
from rr_connector import updater
from rr_connector.agent import Identity, RegistrationError, normalise_url, ws_url
from rr_connector.mt5link import ConnectError, MT5Link, server_check, validate_inputs
from rr_connector.terminals import Terminal, find_terminals

PF = r"C:\Program Files"


def fs(*exes):
    s = {e.replace("/", "\\").lower() for e in exes}
    return lambda p: p.replace("/", "\\").lower() in s


# ---- detection --------------------------------------------------------------------------------------
def test_detects_terminals_from_registry_folders_appdata_and_processes(tmp_path):
    a, b, c = PF + r"\MetaTrader 5", PF + r"\ICMarkets MT5 Terminal", r"D:\Portable\MT5"
    roots_dir = tmp_path
    (roots_dir / "ICMarkets MT5 Terminal").mkdir()
    (roots_dir / "Notepad++").mkdir()
    reg = [{"DisplayName": "MetaTrader 5", "Publisher": "MetaQuotes Ltd.", "InstallLocation": a, "DisplayVersion": "5.0.4755"},
           {"DisplayName": "7-Zip", "InstallLocation": r"C:\Program Files\7-Zip"}]
    exists = fs(a + r"\terminal64.exe", b + r"\terminal64.exe", c + r"\terminal64.exe",
                str(roots_dir / "ICMarkets MT5 Terminal" / "terminal64.exe"))
    ts = find_terminals(registry=lambda: reg, roots=lambda: [roots_dir], origins=lambda: [a, c],
                        processes=lambda: [{"pid": 42, "exe": c + r"\terminal64.exe"}], exists=exists,
                        version_of=lambda e: "")
    by = {t.folder.lower(): t for t in ts}
    assert ts[0].folder == c and ts[0].running and ts[0].pid == 42          # running terminal first
    assert by[a.lower()].version == "5.0.4755" and set(by[a.lower()].sources) == {"registry", "appdata"}
    assert any("icmarkets" in k for k in by) and "7-zip" not in " ".join(by)
    assert len(ts) == 3


def test_no_terminal_installed_gives_clear_instruction():
    link = MT5Link(MT5Adapter(FakeMT5()))
    with pytest.raises(ConnectError) as e:
        link.connect("Demo-Server", 443, [])
    assert e.value.code == "NO_TERMINAL" and "metatrader5.com" in str(e.value)


# ---- connect / validation ---------------------------------------------------------------------------
def term(path="C:/MT5", running=True):
    return Terminal(folder=path, exe=f"{path}/terminal64.exe", running=running)


@pytest.mark.parametrize("addr,port", [("", 443), ("bad host!", 443), ("srv", "x"), ("srv", 0), ("srv", 70000)])
def test_input_validation(addr, port):
    with pytest.raises(ConnectError):
        validate_inputs(addr, port)


def test_connect_matches_server_name_and_reads_account():
    fake = FakeMT5(login=5551, server="Broker-Demo")
    att = MT5Link(MT5Adapter(fake)).connect("broker-demo", "443", [term()])
    assert att.server_check["result"] == "MATCH" and att.mode == "DEMO" and att.account["login"] == 5551


def test_connect_validates_address_by_reachability_and_refuses_unreachable():
    fake = FakeMT5()
    ok = MT5Link(MT5Adapter(fake), prober=lambda a, p: {"reachable": True, "latency_ms": 12.5}).connect("185.1.2.3", 443, [term()])
    assert ok.server_check["result"] == "REACHABLE" and "Demo-Server" in ok.server_check["detail"]
    with pytest.raises(ConnectError) as e:
        MT5Link(MT5Adapter(fake), prober=lambda a, p: {"reachable": False, "error": "timed out"}).connect("185.1.2.3", 443, [term()])
    assert e.value.code == "UNREACHABLE" and "Demo-Server" in str(e.value)


def test_not_logged_in_terminal_never_guesses_credentials():
    fake = FakeMT5()
    fake.logged_in = False
    calls = []
    orig = fake.initialize
    fake.initialize = lambda **kw: calls.append(kw) or orig(**kw)
    with pytest.raises(ConnectError) as e:
        MT5Link(MT5Adapter(fake)).connect("Demo-Server", 443, [term()])
    assert e.value.code == "NOT_LOGGED_IN" and "password" in str(e.value)
    assert calls and all("login" not in c and "password" not in c for c in calls)


def test_terminal_is_started_when_installed_but_not_running():
    fake = FakeMT5()
    launched = []
    att = MT5Link(MT5Adapter(fake), launcher=launched.append, sleep=lambda s: None).connect(
        "Demo-Server", 443, [term(running=False)], launch_wait_s=5)
    assert launched == ["C:/MT5/terminal64.exe"] and att.account["login"] == 1001


def test_several_logged_in_terminals_ask_the_user_to_choose():
    fake = FakeMT5()
    with pytest.raises(ConnectError) as e:
        MT5Link(MT5Adapter(fake)).connect("other-server", 443, [term("C:/A"), term("C:/B")])
    assert e.value.code == "CHOOSE_TERMINAL" and len(e.value.choices) == 2
    att = MT5Link(MT5Adapter(fake), prober=lambda a, p: {"reachable": True, "latency_ms": 1}).connect(
        "other-server", 443, [term("C:/A"), term("C:/B")], chosen_exe="C:/B/terminal64.exe")
    assert att.terminal.folder == "C:/B"


def test_server_check_is_case_insensitive_match():
    assert server_check("ICMarkets-Demo", 443, "icmarkets-demo", lambda a, p: {})["result"] == "MATCH"


# ---- guard ------------------------------------------------------------------------------------------
ACCT = {"login": 7, "server": "S", "equity": 9_600.0, "trade_mode": 0}
BUY = {"action": "MARKET_BUY", "volume": 0.5}


def test_guard_demo_allows_within_limits_and_enforces_caps():
    L = g.Limits(1.0, 3, 5.0)
    g.check(BUY, ACCT, 0, 10_000, L, None, False)                           # 4% loss < 5%
    with pytest.raises(g.Blocked, match="lots"):
        g.check({**BUY, "volume": 1.5}, ACCT, 0, 10_000, L, None, False)
    with pytest.raises(g.Blocked, match="positions"):
        g.check(BUY, ACCT, 3, 10_000, L, None, False)
    with pytest.raises(g.Blocked, match="daily loss"):
        g.check(BUY, {**ACCT, "equity": 9_400}, 0, 10_000, L, None, False)
    g.check({"action": "CLOSE"}, {**ACCT, "equity": 1}, 99, 10_000, L, None, True)  # closing always allowed


def test_guard_live_requires_authorisation_bound_to_the_account():
    live = {**ACCT, "trade_mode": 2}
    L = g.Limits()
    for action in ("MARKET_BUY", "CLOSE", "MODIFY_SLTP"):
        with pytest.raises(g.Blocked, match="not been authorised"):
            g.check({"action": action, "volume": 0.1}, live, 0, None, L, None, False)
    with pytest.raises(g.Blocked):
        g.check(BUY, live, 0, None, L, {"login": 8, "server": "S"}, False)   # other account
    g.check(BUY, live, 0, None, L, {"login": 7, "server": "S"}, False)
    assert g.account_mode({"trade_mode": 99}) == "LIVE"                      # unknown is treated as live
    with pytest.raises(g.Blocked, match="emergency"):
        g.check(BUY, ACCT, 0, None, L, None, True)


# ---- store / identity --------------------------------------------------------------------------------
def test_config_values_are_encrypted_at_rest(store, home, vault):
    store.update(engine_url="https://engine.example.com", mt5_address="ICMarkets-Live")
    raw = b"".join(r[0] for r in sqlite3.connect(str(home / "config.db")).execute("SELECT value FROM settings"))
    assert b"engine.example.com" not in raw and b"ICMarkets" not in raw
    assert store.get("engine_url") == "https://engine.example.com" and store.get("mt5_port") == 443
    assert vault.get("config-key")


def test_device_identity_is_stable_and_private_key_only_in_vault(store, vault, home):
    a, b = Identity(store, vault), Identity(store, vault)
    assert a.device_id == b.device_id and a.public_key == b.public_key
    assert "PRIVATE KEY" in vault.get("device-key")
    assert not any(b"PRIVATE" in p.read_bytes() for p in home.rglob("*") if p.is_file())


def test_engine_url_rules():
    assert normalise_url("engine.example.com/") == "https://engine.example.com"
    assert ws_url("https://e.x") == "wss://e.x/api/v1/mt5/agent/ws"
    with pytest.raises(RegistrationError, match="HTTPS"):
        normalise_url("http://engine.example.com")


# ---- updater ------------------------------------------------------------------------------------------
def test_updater_refuses_a_file_with_the_wrong_hash(store):
    payload = b"MZ installer bytes"
    t = httpx.MockTransport(lambda req: httpx.Response(200, content=payload))
    good = {"url": "https://example.com/setup.exe", "sha256": hashlib.sha256(payload).hexdigest()}
    assert updater.download_and_verify(good, store, transport=t).read_bytes() == payload
    with pytest.raises(ValueError, match="SHA-256"):
        updater.download_and_verify({**good, "sha256": "0" * 64}, store, transport=t)
    with pytest.raises(ValueError, match="HTTPS"):
        updater.download_and_verify({**good, "url": "http://example.com/x.exe"}, store, transport=t)
