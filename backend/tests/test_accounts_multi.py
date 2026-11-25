"""Multi-account foundation: per-account name, trading switch, eligible symbols and strategies, and the account's own
risk limits over its mode's template. Without per-account values everything behaves exactly as before."""
from sqlalchemy import select

from app.db.models import AuditLog, TradeOrder, TradingAccount, User
from app.db.session import session_scope
from app.execution import manager as em
from app.scanner.service import scan_once
from app.services import readiness
from app.services import trading as tr
from app.workers import main as worker
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto


def demo_id():
    with session_scope() as db:
        return db.scalar(select(TradingAccount.id).where(TradingAccount.mode == "DEMO"))


def test_without_own_limits_an_account_uses_its_mode_template(stack):
    with session_scope() as db:
        a = db.get(TradingAccount, demo_id())
        assert a.risk_overrides == {} and a.allowed_symbols is None and a.allowed_strategies is None
        assert tr.account_risk_params(db, a).__dict__ == tr.risk_params(db, "DEMO").__dict__
        a.risk_overrides = {"max_lot_size": 0.02, "not_a_limit": 9}
        eff = tr.account_risk_params(db, a)
        assert eff.max_lot_size == 0.02 and eff.max_daily_loss_pct == tr.risk_params(db, "DEMO").max_daily_loss_pct
        assert not hasattr(eff, "not_a_limit")


def test_activating_an_account_makes_it_an_execution_account(client):
    login(client, "admin")
    r = client.post("/api/accounts", json={"login": 3003, "server": "Demo-2", "mode": "DEMO"}, headers=h(client))
    a = r.json()
    assert a["trading_enabled"] is False and a["label"] == "" and a["own_risk_limits"] == []
    assert client.post(f"/api/accounts/{a['id']}/activate", headers=h(client)).json()["trading_enabled"] is True


def test_account_settings_are_validated_and_audited(client, stack):
    login(client, "admin")
    aid = demo_id()
    url = f"/api/accounts/{aid}/settings"
    r = client.put(url, json={"label": "DEMO Account 1", "allowed_symbols": ["EURUSD.r"],
                              "allowed_strategies": []}, headers=h(client))
    assert r.status_code == 400                                          # empty list: use trading_enabled=false
    assert client.put(url, json={"allowed_symbols": ["NOPE.r"]}, headers=h(client)).status_code == 400
    r = client.put(url, json={"label": "DEMO Account 1", "allowed_symbols": ["EURUSD.r"]}, headers=h(client))
    assert r.status_code == 200 and r.json()["label"] == "DEMO Account 1" and r.json()["allowed_symbols"] == ["EURUSD.r"]
    r = client.put(url, json={"clear_allowed_symbols": True}, headers=h(client))
    assert r.json()["allowed_symbols"] is None
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ACCOUNT_SETTINGS"))
    login(client, "trader")                                              # system.manage only
    assert client.put(url, json={"label": "x"}, headers=h(client)).status_code == 403


def test_enabling_trading_needs_verification_and_for_live_the_approval(client):
    login(client, "admin")
    lid = client.post("/api/accounts", json={"login": 4004, "server": "Live-9", "mode": "LIVE"},
                      headers=h(client)).json()["id"]
    url = f"/api/accounts/{lid}/settings"
    assert client.put(url, json={"trading_enabled": True}, headers=h(client)).status_code == 409   # not verified
    with session_scope() as db:
        db.get(TradingAccount, lid).verified = True
    r = client.put(url, json={"trading_enabled": True}, headers=h(client))
    assert r.status_code == 409 and "APPROVE" in r.text                                       # not approved
    with session_scope() as db:
        db.get(TradingAccount, lid).live_approved_by = db.scalar(select(User.id).where(User.username == "admin"))
    assert client.put(url, json={"trading_enabled": True}, headers=h(client)).json()["trading_enabled"] is True
    with session_scope() as db:
        assert tr.get_state(db).active_mode == "DEMO", "enabling an account never switches the engine to LIVE"
    assert client.put(url, json={"trading_enabled": False}, headers=h(client)).json()["trading_enabled"] is False


def test_account_risk_limits_api(client, stack):
    login(client, "admin")
    aid = demo_id()
    url = f"/api/accounts/{aid}/risk"
    base = client.get(url).json()
    assert base["overrides"] == {} and base["effective"] == base["template"]
    assert client.put(url, json={"risk_per_trade_pct": 50}, headers=h(client)).status_code == 400
    assert client.put(url, json={"bogus": 1}, headers=h(client)).status_code == 400
    r = client.put(url, json={"max_lot_size": 0.03, "max_open_positions": 2}, headers=h(client)).json()
    assert r["overrides"] == {"max_lot_size": 0.03, "max_open_positions": 2} and r["effective"]["max_lot_size"] == 0.03
    assert r["template"]["max_lot_size"] == base["template"]["max_lot_size"], "the mode template is unchanged"
    assert client.put(url, json={}, headers=h(client)).json()["effective"] == base["template"]
    login(client, "trader")                                              # risk.manage only
    assert client.put(url, json={"max_lot_size": 1}, headers=h(client)).status_code == 403


def test_the_risk_engine_applies_the_accounts_own_limits(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        db.get(TradingAccount, demo_id()).risk_overrides = {"max_lot_size": 0.01}
    with session_scope() as db:
        o = open_eur(db, bridge, key="acct-lim")
        assert o.status == "CONFIRMED" and float(o.volume) <= 0.01, (o.status, o.volume)


def scan_with_engine_only(scan, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = "ENGINE_ONLY"
    return fake, bridge


def test_auto_trading_respects_the_accounts_symbols(scan, monkeypatch):
    fake, bridge = scan_with_engine_only(scan, monkeypatch)
    with session_scope() as db:
        db.get(TradingAccount, demo_id()).allowed_symbols = ["NOT-TRADED.r"]
    scan_once(bridge, None, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        assert not db.scalars(select(TradeOrder)).all() and not fake.positions


def test_a_research_only_account_is_never_auto_traded(scan, monkeypatch):
    fake, bridge = scan_with_engine_only(scan, monkeypatch)
    with session_scope() as db:
        db.get(TradingAccount, demo_id()).trading_enabled = False
    scan_once(bridge, None, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        assert not db.scalars(select(TradeOrder)).all() and not fake.positions


def test_readiness_checks_the_live_accounts_own_limits(db_clean):
    from tests.test_part29 import approved_live_account
    from app.cli import seed  # noqa: F401
    with session_scope() as db:
        from app.db.models import BrokerConnection, MT5Terminal
        t = MT5Terminal(name="t-multi", bridge_url="")
        db.add(t)
        db.flush()
        bc = BrokerConnection(broker_name="B", server="Demo-Server", terminal_id=t.id, timezone="Etc/GMT-2")
        db.add(bc)
        db.flush()
        db.add(TradingAccount(broker_connection_id=bc.id, login=1001, server="Demo-Server", mode="DEMO"))
    approved_live_account()
    with session_scope() as db:
        items = {i["key"]: i for i in readiness.report(db)["items"]}
        assert items["live_risk_config"]["passed"]
        live = db.scalar(select(TradingAccount).where(TradingAccount.mode == "LIVE"))
        live.risk_overrides = {"risk_per_trade_pct": tr.risk_params(db, "DEMO").risk_per_trade_pct + 0.5}
    with session_scope() as db:
        items = {i["key"]: i for i in readiness.report(db)["items"]}
        assert items["live_risk_config"]["passed"] is False and "risk_per_trade_pct" in items["live_risk_config"]["detail"]
