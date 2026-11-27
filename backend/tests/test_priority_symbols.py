"""Priority symbols: the owner's high-volatility list is scanned and ranked first; per-symbol spread limits are the
owner's own; priority never loosens a risk limit."""
from sqlalchemy import select

from app.db.models import Symbol
from app.db.session import session_scope
from app.risk import engine as r
from app.scanner import priority as prio
from tests.test_api import h, login  # noqa: F401
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_risk import CLEAR, EURUSD, XAUUSD, acct, req


def test_broker_names_are_matched_with_their_suffixes():
    names = prio.clean(["xauusd", "US30", "us-100", "XAUUSD"])
    assert names == ["XAUUSD", "US30", "US100"]
    assert prio.match("XAUUSDR", names) == "XAUUSD" and prio.match("XAUUSDM", names) == "XAUUSD"
    assert prio.match("US30", names) == "US30" and prio.match("EURUSD", names) is None
    assert prio.match("XAUUSDLONGNAME", names) is None, "not a broker suffix"
    assert "XAUUSD" in prio.DEFAULT and "BTCUSD" in prio.DEFAULT and len(prio.DEFAULT) == 36


def test_a_per_symbol_spread_limit_is_the_owners_and_the_global_limit_stays():
    p = r.RiskParams(news_unknown_blocks=False, max_spread_points=30)
    gold = req("XAUUSD", entry=2400.0, sl=2390.0, tp=2420.0, spread=45)
    assert any("spread 45.0pt > max 30pt" in x for x in r.evaluate(gold, XAUUSD, acct(), [], p, CLEAR).reasons)
    p.symbol_max_spread_points = {"XAUUSD": 50}
    assert r.evaluate(gold, XAUUSD, acct(), [], p, CLEAR).approved
    assert not r.evaluate(req(spread=45), EURUSD, acct(), [], p, CLEAR).approved, "other symbols keep the global limit"
    assert r.RiskParams(symbol_max_spread_points={"X": 0}).validate()
    assert r.RiskParams.from_dict({"symbol_max_spread_points": {"XAUUSD.r": 45}}).symbol_max_spread_points == {"XAUUSD.r": 45}


def test_priority_list_raises_scan_priority_and_enables_tradable_symbols(stack):
    from app.db.models import ScannerSettings
    with session_scope() as db:
        target = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
        target.scan_enabled, target.tradable = False, True
        tid = target.id
        db.merge(ScannerSettings(id=1, timeframes=["H1"], priority_symbols=["EURUSD"]))
    with session_scope() as db:
        out = prio.apply(db)
        t = db.get(Symbol, tid)
        assert t.priority == prio.PRIORITY and t.scan_enabled and "EURUSD.r" in out["scan_enabled_now"]
        assert all(s.priority != prio.PRIORITY for s in db.scalars(select(Symbol)) if s.id != tid)
        t.excluded, t.scan_enabled = True, False
    with session_scope() as db:
        prio.apply(db)
        assert not db.get(Symbol, tid).scan_enabled, "an excluded symbol is never switched back on"
        db.get(ScannerSettings, 1).priority_symbols = ["NOTHING"]
    with session_scope() as db:
        out = prio.apply(db)
        assert db.get(Symbol, tid).priority == prio.NORMAL and out["not_at_broker"] == ["NOTHING"]


def test_priority_api_and_spread_view(stack):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_api import make_user
    make_user("admin", "admin")
    c = TestClient(app)
    login(c, "admin")
    g = c.get("/api/scanner/priority").json()
    assert g["is_default"] and g["list"] == prio.DEFAULT and g["mode"] == "DEMO"
    with session_scope() as db:
        eur = db.scalar(select(Symbol).where(Symbol.broker_symbol == "EURUSD.r"))
    r1 = c.put("/api/scanner/priority", json={"symbols": ["eurusd", "XAUUSD"]}, headers=h(c)).json()
    assert r1["list"] == ["EURUSD", "XAUUSD"] and "EURUSD.r" in r1["found"]["EURUSD"]
    row = next(x for x in r1["symbols"] if x["symbol"] == "EURUSD.r")
    assert row["priority"] == prio.PRIORITY and row["spread_limit"] > 0 and row["own_limit"] is None
    cfg = c.get("/api/risk/config/DEMO").json()
    assert c.put("/api/risk/config/DEMO", json={**cfg, "symbol_max_spread_points": {"EURUSD.r": 12}},
                 headers=h(c)).status_code == 200
    row = next(x for x in c.get("/api/scanner/priority").json()["symbols"] if x["symbol"] == "EURUSD.r")
    assert row["own_limit"] == 12 and row["spread_limit"] == 12
    assert c.put("/api/scanner/priority", json={"symbols": None}, headers=h(c)).json()["list"] == prio.DEFAULT
    with session_scope() as db:
        assert db.get(Symbol, eur.id).priority == prio.NORMAL
