"""LIVE readiness checklist (Parts 27-30 Phase 6, Part 29.10).

The engine's switch to LIVE (``POST /api/control/mode`` with the typed phrase) already needs a verified,
administrator-approved LIVE account and no unresolved executions. This checklist adds the rest of Part 29.10. Every
**required** item must pass before the switch is accepted; **informational** items are shown but do not block.
Nothing here enables LIVE: it can only refuse.

Required:
* identity: an active LIVE account, verified against the broker and approved by an administrator;
* LIVE risk configuration: valid, and no looser than DEMO's on risk per trade, daily loss, drawdown and lot size;
* emergency controls: kill switch and risk lock clear, a valid emergency policy;
* executions resolved: no pending, sent or unknown order;
* DEMO acceptance: at least ``RR_LIVE_MIN_DEMO_TRADES`` (default 20) closed DEMO trades on record.

Informational:
* MT5 connection health. Startup reconciliation re-verifies the account after every mode switch anyway.
* The PC connector's own LIVE lock, as its heartbeat reports it. It is released only on the PC and enforced by the
  connector itself.
* Candidate review provider. Without one, nothing is auto-executed in either mode.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ClosedPosition, TradeOrder, TradingAccount
from app.services import trading as tr

TIGHTER = ("risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct", "max_lot_size")


def report(db: Session) -> dict:
    s = get_settings()
    items: list[dict] = []

    def add(key, label, ok, detail, required=True):
        items.append({"key": key, "label": label, "passed": bool(ok) if ok is not None else None, "detail": detail,
                      "required": required})
    acct = db.scalar(select(TradingAccount).where(TradingAccount.mode == "LIVE", TradingAccount.is_active.is_(True)))
    add("identity", "LIVE account verified and approved",
        acct is not None and acct.verified and acct.live_approved_by is not None,
        "no active LIVE account" if acct is None else
        f"account {acct.login}: verified={acct.verified}, approved={'yes' if acct.live_approved_by else 'no'}")
    live, demo = tr.risk_params(db, "LIVE"), tr.risk_params(db, "DEMO")
    errs = live.validate()
    looser = [k for k in TIGHTER if getattr(live, k) > getattr(demo, k)]
    add("live_risk_config", "LIVE risk configuration valid and no looser than DEMO", not errs and not looser,
        "; ".join(errs) if errs else (f"looser than DEMO: {', '.join(looser)}" if looser else
                                      ", ".join(f"{k} {getattr(live, k)}" for k in TIGHTER)))
    st = tr.get_state(db)
    add("emergency_controls", "Kill switch, risk lock and emergency policy ready",
        not st.kill_switch and not st.risk_lock and live.existing_position_policy in ("PROTECT", "REDUCE", "CLOSE_ALL"),
        f"kill switch {'ON' if st.kill_switch else 'off'}, risk lock {'ON' if st.risk_lock else 'off'}, "
        f"policy {live.existing_position_policy}")
    pending = db.scalar(select(func.count(TradeOrder.id)).where(TradeOrder.status.in_(["PENDING", "SENT", "UNKNOWN"]))) or 0
    add("executions_resolved", "No unresolved executions", pending == 0, f"{pending} unresolved")
    demo_ids = [a.id for a in db.scalars(select(TradingAccount).where(TradingAccount.mode == "DEMO"))]
    n, pnl = 0, 0.0
    if demo_ids:
        n, pnl = db.execute(select(func.count(ClosedPosition.id), func.coalesce(func.sum(ClosedPosition.profit), 0.0))
                            .where(ClosedPosition.account_id.in_(demo_ids))).one()
    need = s.live_min_demo_trades
    add("demo_acceptance", f"At least {need} closed DEMO trades", int(n) >= need,
        f"{int(n)} closed DEMO trades, net {float(pnl):.2f} (review the DEMO results before approving)")
    health = None
    try:
        from app.services.events import get_redis
        raw = get_redis().get("rr:bridge:health")
        health = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        pass
    add("connection", "MT5 connection", None if health is None else bool(health.get("connected")),
        "no recent heartbeat" if health is None else ("connected" if health.get("connected") else
                                                       f"disconnected: {health.get('error', '')}"), required=False)
    # Parts 27-30 Phase 10: the connector reports its own lock in its heartbeat. It is released only on the PC
    # (RR Trading One window -> LIVE on this PC, typed account number and phrase) and enforced by the connector itself.
    try:
        from app.execution import relay
        agent = relay.select_agent(db)
        ast = (agent.status or {}) if agent else {}
    except Exception:  # noqa: BLE001
        agent, ast = None, {}
    pc = bool(ast.get("live_authorized"))
    add("pc_connector_lock", "PC connector LIVE lock", None if agent is None else pc,
        "no MT5 connector registered" if agent is None else
        ("authorised on the PC for the connected LIVE account" if pc else
         "locked on the PC (release it only in the RR Trading One window: LIVE on this PC, typed account number and phrase)"),
        required=False)
    from app.ai.gateway import configured
    if (st.ai_mode or "AI") == "ENGINE_ONLY":
        add("ai_review", "Candidate review provider", None,
            "engine-only mode (Run without AI): no AI review; the engine's BUY/SELL signals go to the Risk Engine. "
            "Switching LIVE to engine-only needs its own typed confirmation", required=False)
    else:
        add("ai_review", "Candidate review provider", configured("candidate_review"),
            "configured" if configured("candidate_review") else
            "none: candidates stay unreviewed and are never auto-executed (or choose Run without AI)", required=False)
    failing = [i["key"] for i in items if i["required"] and not i["passed"]]
    return {"ready": not failing, "failing": failing, "items": items}
