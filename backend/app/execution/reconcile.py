"""Pending-order reconciliation and emergency cancellation (Parts 27-30 Phase 7, Part 30.1 and 30.4).

The MT5 connector stamps every order it sends with ``magic`` = ``MAGIC`` and a comment ``tag(execution_id)``
(``mt5-connector/connector/service.py``: the same constant and function, repeated here because the engine and the
connector are separate programs; ``tests/test_part30.py`` checks they agree).

``compare_pending`` matches the broker's pending orders with the engine's journal:
* **matched:** engine orders the journal knows;
* **unknown_engine:** orders with the engine's tag that the journal does not know (for example after restoring an
  older database). Startup reconciliation fails on these, so new entries stay blocked until an operator resolves
  them; the engine does not guess.
* **foreign:** orders placed by someone else (manual or another program). These are reported, never touched.
* **missing_at_broker:** confirmed pending orders in the journal that are no longer at the broker (filled, cancelled
  or expired). These are reported.

``cancel_engine_pending`` (kill switch) cancels only the engine's own pending orders, each as a journalled,
idempotent CANCEL_PENDING command. Cancelling reduces risk, so it is allowed under the kill switch. Foreign orders
are never cancelled.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

MAGIC = 26_062_026
PENDING_ACTIONS = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")


def tag(execution_id: str) -> str:
    return "RR" + hashlib.sha1(execution_id.encode()).hexdigest()[:14]


def compare_pending(db: Session, broker_orders: list[dict], account) -> dict:
    from app.db.models import TradeOrder
    journal = db.scalars(select(TradeOrder).where(TradeOrder.account_id == account.id,
                                                 TradeOrder.action.in_(PENDING_ACTIONS),
                                                 TradeOrder.status == "CONFIRMED")).all()
    by_tag = {tag(o.execution_id): o for o in journal}
    matched, unknown, foreign, seen = [], [], [], set()
    for b in broker_orders:
        t = str(b.get("comment") or "")
        entry = {"ticket": b.get("ticket"), "symbol": b.get("symbol"), "type": b.get("type"),
                 "volume": b.get("volume_current", b.get("volume_initial")), "price": b.get("price_open")}
        if int(b.get("magic") or 0) == MAGIC and t.startswith("RR"):
            o = next((o for k, o in by_tag.items() if t.startswith(k)), None)
            if o is not None:
                matched.append({**entry, "execution_id": o.execution_id})
                seen.add(o.execution_id)
            else:
                unknown.append(entry)
        else:
            foreign.append(entry)
    missing = [{"execution_id": o.execution_id, "symbol": o.symbol, "action": o.action}
               for o in journal if o.execution_id not in seen]
    return {"matched": matched, "unknown_engine": unknown, "foreign": foreign, "missing_at_broker": missing,
            "ok": not unknown}


def cancel_engine_pending(db: Session, bridge, account, reason: str, user_id: int | None = None) -> list[dict]:
    from app.db.models import TradeOrder
    from app.execution import manager as em
    from app.services.events import audit
    out = []
    try:
        broker = bridge.orders()
    except Exception as exc:  # noqa: BLE001 - reported; the kill switch itself is already set
        return [{"action": "CANCEL_PENDING", "error": f"pending orders unavailable: {exc}"}]
    for b in broker:
        if int(b.get("magic") or 0) != MAGIC or not str(b.get("comment") or "").startswith("RR"):
            continue
        ticket = int(b["ticket"])
        eid, existing = em._reserve(db, f"{account.id}:cancel-pending:{ticket}")
        if existing is not None:
            out.append({"action": "CANCEL_PENDING", "order": ticket, "status": existing.status})
            continue
        order = TradeOrder(execution_id=eid, account_id=account.id, symbol=str(b.get("symbol")), action="CANCEL_PENDING",
                           volume=Decimal("0"), source="SYSTEM", requested_by=user_id)
        db.add(order)
        db.flush()
        audit(db, user_id, "PENDING_CANCEL", str(b.get("symbol")), {"order": ticket, "reason": reason})
        em._send(db, bridge, order, account, {"order_ticket": ticket})
        out.append({"action": "CANCEL_PENDING", "order": ticket, "status": order.status})
    return out
