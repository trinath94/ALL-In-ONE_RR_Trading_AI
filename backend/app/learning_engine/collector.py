"""Historical trade collector.

* Incrementally imports **MT5 deals** (the broker's executed facts) into ``mt5_deals``; unique per
  (account, deal ticket) so re-syncs never duplicate. A changed deal (broker correction) is updated.
* Aggregates deals into position outcomes in the existing ``closed_positions`` table (single source of
  truth): volume-weighted entry/exit, multiple partial exits, commissions/swaps/fees from every deal.
  A position is only recorded as closed when exit volume covers entry volume **and** MT5 no longer
  reports it open — an accepted order request is never treated as a trade.
* DEMO and LIVE are separated by account and by the ``mode`` column.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import ClosedPosition, OpenPosition, Symbol, TradeOrder, TradingAccount
from app.db.models_intel import MT5Deal, SyncState
from app.execution.bridge import BridgeClient, BridgeError

log = logging.getLogger("learning.collector")
UTC = timezone.utc
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
TRADE_DEAL_TYPES = (0, 1)    # DEAL_TYPE_BUY / DEAL_TYPE_SELL; balance/credit/etc. are excluded
OVERLAP = timedelta(hours=6)  # re-read a window to catch late-arriving or corrected deals
INCONSISTENT = "INCONSISTENT"  # sync_status of a position whose deals do not reconcile; excluded from learning


def _ts(v) -> datetime:
    return datetime.fromtimestamp(int(v), UTC)


def import_deals(db: Session, account: TradingAccount, deals: list[dict]) -> dict:
    inserted = updated = skipped = 0
    for d in deals:
        if int(d.get("type", -1)) not in TRADE_DEAL_TYPES:
            skipped += 1
            continue
        row = {
            "account_id": account.id, "mode": account.mode, "deal_ticket": int(d["ticket"]),
            "order_ticket": int(d.get("order") or 0) or None, "position_id": int(d.get("position_id") or 0),
            "symbol": d["symbol"], "deal_type": int(d["type"]), "entry": int(d.get("entry", 0)),
            "reason": int(d.get("reason", -1)), "volume": float(d["volume"]), "price": float(d["price"]),
            "profit": float(d.get("profit") or 0), "commission": float(d.get("commission") or 0),
            "swap": float(d.get("swap") or 0), "fee": float(d.get("fee") or 0), "magic": int(d.get("magic") or 0),
            "comment": (d.get("comment") or "")[:64], "deal_time": _ts(d["time"]),
            "raw": {k: v for k, v in d.items() if isinstance(v, (int, float, str, bool))},
        }
        existing = db.scalar(select(MT5Deal).where(MT5Deal.account_id == account.id, MT5Deal.deal_ticket == row["deal_ticket"]))
        if existing is None:
            db.execute(insert(MT5Deal).values(**row).on_conflict_do_nothing(constraint="uq_mt5_deal"))
            inserted += 1
        else:
            changed = any(getattr(existing, k) != v for k, v in row.items() if k not in ("raw", "deal_time"))
            if changed:
                for k, v in row.items():
                    setattr(existing, k, v)
                existing.synced_at = datetime.now(UTC)
                updated += 1
    db.flush()
    return {"inserted": inserted, "updated": updated, "skipped_non_trade": skipped}


def aggregate_position(deals: list[MT5Deal]) -> dict | None:
    """Combine all deals of one position. Returns None while the position is still open."""
    ins = [d for d in deals if d.entry in (ENTRY_IN, ENTRY_INOUT)]
    outs = [d for d in deals if d.entry in (ENTRY_OUT, ENTRY_OUT_BY, ENTRY_INOUT)]
    if not ins or not outs:
        return None
    in_vol = sum(d.volume for d in ins if d.entry == ENTRY_IN) or sum(d.volume for d in ins)
    out_vol = sum(d.volume for d in outs)
    vwap = lambda ds: sum(d.price * d.volume for d in ds) / sum(d.volume for d in ds)  # noqa: E731
    first_in = min(ins, key=lambda d: d.deal_time)
    last_out = max(outs, key=lambda d: d.deal_time)
    reason = {4: "STOP_LOSS", 5: "TAKE_PROFIT", 6: "STOP_OUT"}.get(last_out.reason, "CLOSED")
    problems = []
    if out_vol > in_vol + 1e-9:
        problems.append(f"exit volume {out_vol:g} > entry volume {in_vol:g}")
    if any(d.deal_time < first_in.deal_time for d in outs if d.entry != ENTRY_INOUT):
        problems.append("exit deal precedes the first entry")
    if any(d.deal_type == first_in.deal_type for d in outs if d.entry == ENTRY_OUT):
        problems.append("exit deal on the same side as the entry")
    if len({d.symbol for d in deals}) > 1:
        problems.append("deals on more than one symbol")
    return {
        "position_id": first_in.position_id, "symbol": first_in.symbol,
        "direction": "BUY" if first_in.deal_type == 0 else "SELL",
        "volume": in_vol, "open_price": vwap([d for d in ins if d.entry == ENTRY_IN] or ins), "close_price": vwap(outs),
        "profit": sum(d.profit for d in deals), "commission": sum(d.commission + d.fee for d in deals),
        "swap": sum(d.swap for d in deals), "opened_at": first_in.deal_time, "closed_at": last_out.deal_time,
        "partial_exits": len(outs), "complete": out_vol + 1e-9 >= in_vol, "close_reason": reason,
        "comment": first_in.comment, "problems": problems,
    }


def upsert_closed(db: Session, account: TradingAccount, agg: dict) -> tuple[ClosedPosition, str]:
    from app.execution.manager import mt5_tag
    cp = db.scalar(select(ClosedPosition).where(ClosedPosition.account_id == account.id,
                                                ClosedPosition.ticket == agg["position_id"]))
    order = None
    if agg["comment"].startswith("RR"):
        for o in db.scalars(select(TradeOrder).where(TradeOrder.account_id == account.id,
                                                     TradeOrder.action.in_(["MARKET_BUY", "MARKET_SELL", "BUY_LIMIT",
                                                                            "SELL_LIMIT", "BUY_STOP", "SELL_STOP"]))):
            if mt5_tag(o.execution_id) == agg["comment"]:
                order = o
                break
    stop = float(order.stop_loss) if order and order.stop_loss else (float(cp.stop_loss) if cp and cp.stop_loss else None)
    r_mult = None
    if stop:
        risk = abs(agg["open_price"] - stop)
        if risk > 0:
            r_mult = (agg["close_price"] - agg["open_price"]) * (1 if agg["direction"] == "BUY" else -1) / risk
    fields = dict(symbol=agg["symbol"], direction=agg["direction"], volume=Decimal(str(agg["volume"])),
                  open_price=Decimal(str(agg["open_price"])), close_price=Decimal(str(agg["close_price"])),
                  stop_loss=Decimal(str(stop)) if stop else None, profit=agg["profit"], commission=agg["commission"],
                  swap=agg["swap"], r_multiple=r_mult, opened_at=agg["opened_at"], closed_at=agg["closed_at"],
                  close_reason=agg["close_reason"], mode=account.mode, partial_exits=agg["partial_exits"])
    if order is not None:
        fields["execution_id"] = order.execution_id
        fields["strategy_codes"] = order.strategy_codes or []
    if cp is None:
        cp = ClosedPosition(account_id=account.id, ticket=agg["position_id"], sync_status="COMPLETE", **fields)
        db.add(cp)
        db.flush()
        return cp, "inserted"
    changed = False
    for k, v in fields.items():
        cur = getattr(cp, k)
        if k == "strategy_codes":
            diff = not cur and bool(v)          # only fill attribution if missing, never overwrite it
        elif k in ("mode", "partial_exits", "r_multiple", "stop_loss") and v is None:
            diff = False                        # never erase known information
        elif isinstance(v, float) and cur is not None:
            diff = abs(float(cur) - v) > 1e-6
        else:
            diff = cur != v
        if diff:
            setattr(cp, k, v)
            changed = True
    if changed:
        cp.sync_status = "CORRECTED" if cp.sync_status else "COMPLETE"
        return cp, "corrected"
    if not cp.sync_status:
        cp.sync_status = "COMPLETE"
    return cp, "unchanged"


def sync_account_history(db: Session, bridge: BridgeClient, account: TradingAccount, initial_days: int = 365) -> dict:
    """Incremental sync: from the last synced deal time (minus overlap) to now."""
    state = db.scalar(select(SyncState).where(SyncState.account_id == account.id, SyncState.stream == "deals"))
    if state is None:
        state = SyncState(account_id=account.id, stream="deals")
        db.add(state)
        db.flush()
    now = datetime.now(UTC)
    since = (state.last_time - OVERLAP) if state.last_time else now - timedelta(days=initial_days)
    try:
        deals = bridge.history(int(since.timestamp()), int(now.timestamp()) + 60)
    except BridgeError as exc:
        state.status, state.detail = "ERROR", f"bridge: {exc}"[:500]
        return {"ok": False, "error": str(exc)}
    imported = import_deals(db, account, deals)
    # positions touched in this window
    touched = {int(d.get("position_id") or 0) for d in deals if int(d.get("type", -1)) in TRADE_DEAL_TYPES}
    open_tickets = {p.ticket for p in db.scalars(select(OpenPosition).where(OpenPosition.account_id == account.id))}
    try:
        open_tickets |= {int(p["ticket"]) for p in bridge.positions()}
    except BridgeError:
        pass
    by_pos: dict[int, list[MT5Deal]] = defaultdict(list)
    if touched:
        for d in db.scalars(select(MT5Deal).where(MT5Deal.account_id == account.id, MT5Deal.position_id.in_(touched))):
            by_pos[d.position_id].append(d)
    results = defaultdict(int)
    closed_ids = []
    for pid, ds in by_pos.items():
        agg = aggregate_position(ds)
        if agg is not None and agg["problems"]:
            # Never derive outcomes from a ledger that does not reconcile (e.g. reused position ids):
            # flag it, keep the previously recorded values, and keep it out of learning.
            results["inconsistent"] += 1
            cp = db.scalar(select(ClosedPosition).where(ClosedPosition.account_id == account.id,
                                                        ClosedPosition.ticket == pid))
            if cp is not None and cp.sync_status != INCONSISTENT:
                cp.sync_status = INCONSISTENT
            log.warning("deal ledger for position %s does not reconcile: %s", pid, "; ".join(agg["problems"]))
            continue
        if agg is None or pid in open_tickets or not agg["complete"]:
            results["still_open_or_partial"] += 1
            continue
        cp, what = upsert_closed(db, account, agg)
        results[what] += 1
        closed_ids.append(cp.id)
    if deals:
        state.last_time = max(_ts(d["time"]) for d in deals)
    state.status, state.detail = "OK", ""
    return {"ok": True, **imported, **results, "closed_position_ids": closed_ids}


def unlinked_symbols(db: Session) -> list[str]:
    known = {s for (s,) in db.execute(select(Symbol.broker_symbol))}
    return sorted({s for (s,) in db.execute(select(MT5Deal.symbol))} - known)
