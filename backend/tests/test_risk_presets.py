"""Risk profiles (FTMO-style) and the risk lock reset."""
from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import session_scope
from app.risk.engine import RiskParams
from app.risk.presets import PRESETS
from app.services import trading as tr
from tests.test_api import client, h, login  # noqa: F401 (fixture)


def test_every_profile_is_valid_and_ftmo_stays_inside_the_firms_limits():
    for pid, p in PRESETS.items():
        assert not RiskParams.from_dict({**RiskParams().__dict__, **p["values"]}).validate(), pid
    v = PRESETS["ftmo"]["values"]
    assert v["max_daily_loss_pct"] < 5 and v["max_drawdown_pct"] < 10, "buffers below FTMO's 5 % / 10 %"
    assert v["max_floating_loss_pct"] > 0 and v["require_take_profit"] and v["news_unknown_blocks"]
    assert "initial_capital" not in v and "max_spread_points" not in v, "account-specific values are kept"


def test_profile_is_served_then_saved_by_the_user(client):
    login(client, "viewer")
    ps = {p["id"]: p for p in client.get("/api/risk/presets").json()}
    assert ps["ftmo"]["valid"] and "FTMO" in ps["ftmo"]["label"]
    login(client, "admin")
    cur = client.get("/api/risk/config/DEMO").json()
    saved = client.put("/api/risk/config/DEMO", json={**cur, "initial_capital": 100000, **ps["ftmo"]["values"]},
                       headers=h(client)).json()
    assert saved["max_daily_loss_pct"] == 4.0 and saved["max_drawdown_pct"] == 9.0 and saved["initial_capital"] == 100000
    assert saved["max_spread_points"] == cur["max_spread_points"]


def test_risk_lock_reset_needs_permission_and_confirmation_and_keeps_the_kill_switch(client):
    with session_scope() as db:
        st = tr.get_state(db)
        st.risk_lock, st.risk_lock_reason = True, "daily loss 4.1% >= 4.0%"
        st.kill_switch, st.kill_reason = True, "manual"
    login(client, "trader")
    assert client.post("/api/control/risk-lock/reset", json={"confirm_text": "RESET RISK LOCK"},
                       headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post("/api/control/risk-lock/reset", json={"confirm_text": "yes"}, headers=h(client)).status_code == 428
    r = client.post("/api/control/risk-lock/reset", json={"confirm_text": "RESET RISK LOCK"}, headers=h(client)).json()
    assert r["risk_lock"] is False and "daily loss" in r["note"] and r["kill_switch"] is True, "the kill switch is separate"
    with session_scope() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == "RISK_LOCK_RESET"))
        assert a and a.detail["reason"].startswith("daily loss")
    again = client.post("/api/control/risk-lock/reset", json={"confirm_text": "RESET RISK LOCK"}, headers=h(client)).json()
    assert "was not set" in again["note"]
