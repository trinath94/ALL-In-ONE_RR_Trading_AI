"""System check agent: is every part of the engine working, and if not, why and what to do.

It reuses the operator agent's full inspection (``app.services.agent.inspect``: database and migrations, Redis,
services, MT5 connectors and accounts, broker symbols, AI and budget, risk configuration, trading controls,
strategies, runtime, open incidents) and adds live probes of the MT5 connector and the market data the execution path
needs. The result is one status per system (ok / warning / failing) with the evidence, the impact on trading and the
fix (a page or desktop step), which the console shows in a pop-up when something is failing.

``test_trade`` proves the whole execution chain with one real DEMO order on the connected broker account: pre-flight
check, Risk Engine approval (every control applies: kill switch, risk lock, mandatory stop loss, limits), order sent
through the MT5 connector, broker fill, position visible, then the position is closed again and the close confirmed.
It is refused in LIVE. The order is the broker's minimum volume with a stop loss, is tagged ``TEST`` (never attributed
to a strategy and never counted toward LIVE readiness), and costs the spread.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OpenPosition, Symbol, TradeOrder
from app.services import agent
from app.services import trading as tr

UTC = timezone.utc
TEST_SOURCE = "TEST"

# system -> (title, agent check components it covers, where to fix)
SYSTEMS = [
    ("database", "Database", ("database",), "Desktop app → Start All; System Monitor"),
    ("services", "Engine services and Redis", ("services", "runtime"), "Desktop app → Start All / Recovery"),
    ("mt5", "MT5 connector and broker account", ("accounts",), "MT5 Manager (desktop app); Settings → Accounts"),
    ("market_data", "Broker symbols and market data", ("symbols",), "Broker Symbols page"),
    ("risk", "Risk Engine and trading controls", ("risk", "controls"), "Control Center; Risk settings"),
    ("strategies", "Strategies", ("strategies",), "Strategy Library"),
    ("ai", "AI (Anthropic API)", ("ai",), "AI Studio"),
    ("incidents", "Open incidents", ("incidents",), "System Monitor → Incidents"),
]
SEVERITY_STATUS = {"critical": "failing", "error": "failing", "warning": "warning", "info": "ok"}
RANK = {"ok": 0, "warning": 1, "failing": 2}


def _probe_mt5(db: Session, bridge) -> list[dict]:
    """Live connector probe: connected, logged in to the configured account, quotes arriving."""
    out = []
    if bridge is None:
        return [agent.finding("accounts", "critical", "MT5 connector not configured", "the engine has no connector "
                              "address/secret", "No order can reach the broker.", "user",
                              "Desktop app → MT5 Manager → Link and Start the connector.")]
    try:
        h = bridge.health()
    except Exception as exc:  # noqa: BLE001
        return [agent.finding("accounts", "critical", "MT5 connector not reachable", f"{type(exc).__name__}: {exc}",
                              "No order can reach the broker; positions are not protected by the engine.", "user",
                              "Open MT5 and log in, then desktop app → MT5 Manager → Start the connector.")]
    if not h.get("connected"):
        out.append(agent.finding("accounts", "critical", "MT5 terminal not connected", f"connector health: {h}",
                                 "No order can reach the broker.", "user",
                                 "Open the MT5 terminal, log in to the broker, keep it running."))
    elif not h.get("identity_ok"):
        out.append(agent.finding("accounts", "critical", "MT5 logged in to a different account", f"connector health: {h}",
                                 "Execution is blocked so no order reaches the wrong account.", "user",
                                 "Log the MT5 terminal in to the configured account (MT5 Manager shows which)."))
    sym = pick_symbol(db)
    if sym is None:
        out.append(agent.finding("symbols", "error", "No tradable symbol", "no scan-enabled, tradable broker symbol",
                                 "Nothing can be scanned or traded.", "user", "Broker Symbols → refresh and enable "
                                 "symbols."))
    elif h.get("connected"):
        try:
            t = bridge.tick(sym.broker_symbol)
            age = datetime.now(UTC).timestamp() - float(t.get("time") or 0)
            if not t.get("bid") or not t.get("ask"):
                out.append(agent.finding("symbols", "warning", f"No quote for {sym.broker_symbol}",
                                         f"tick: {t}", "No entry is possible while the market is closed.", "none",
                                         "Wait for the market to open (forex: Sunday evening to Friday evening)."))
            elif t.get("time") and age > 300:
                out.append(agent.finding("symbols", "warning", f"Quotes for {sym.broker_symbol} are stale",
                                         f"last tick {age:.0f} s ago", "Entries are refused on stale quotes.", "none",
                                         "The market is probably closed; otherwise check the MT5 connection."))
        except Exception as exc:  # noqa: BLE001
            out.append(agent.finding("symbols", "error", f"Quote request failed for {sym.broker_symbol}",
                                     f"{type(exc).__name__}: {exc}", "Entries cannot be priced.", "user",
                                     "Check the symbol in MT5 Market Watch and the connector log."))
    return out


def not_test_trade():
    """Filter for ClosedPosition: excludes system test trades (they prove the execution path; they are not trading
    results and never count toward DEMO acceptance or LIVE readiness)."""
    from sqlalchemy import or_

    from app.db.models import ClosedPosition
    tests = select(TradeOrder.execution_id).where(TradeOrder.source == TEST_SOURCE)
    return or_(ClosedPosition.execution_id.is_(None), ClosedPosition.execution_id.notin_(tests))


def pick_symbol(db: Session, name: str | None = None) -> Symbol | None:
    q = select(Symbol).where(Symbol.tradable.is_(True), Symbol.excluded.isnot(True))
    if name:
        return db.scalar(q.where(Symbol.broker_symbol == name))
    rows = db.scalars(q.where(Symbol.scan_enabled.is_(True)).order_by(Symbol.id)).all()
    return next((s for s in rows if s.category == "forex"), rows[0] if rows else None)


def run(db: Session, bridge=None, live: bool = True) -> dict:
    findings, ran = agent.inspect(db, list(agent.CHECKS))
    if live:
        findings += _probe_mt5(db, bridge)
    st = tr.get_state(db)
    systems = []
    for key, title, comps, where in SYSTEMS:
        fs = [f for f in findings if f["component"] in comps]
        status = max((SEVERITY_STATUS.get(f["severity"], "warning") for f in fs), key=RANK.get, default="ok")
        systems.append({"key": key, "title": title, "status": status, "where": where,
                        "problems": [{k: f[k] for k in ("severity", "title", "evidence", "impact", "fix", "fix_kind")}
                                     for f in fs if f["severity"] != "info"]})
    overall = max((s["status"] for s in systems), key=RANK.get, default="ok")
    can_trade = not any(s["status"] == "failing" for s in systems if s["key"] in ("database", "services", "mt5",
                                                                                   "market_data", "risk"))
    return {"at": datetime.now(UTC).isoformat(), "overall": overall, "mode": st.active_mode,
            "auto_trading": st.auto_trading, "can_trade": can_trade, "systems": systems, "checks": ran,
            "fingerprint": "|".join(sorted(f"{f['component']}:{f['title']}" for f in findings
                                           if f["severity"] in ("critical", "error")))}


# ---- end-to-end test trade --------------------------------------------------------------------------------------------
def test_trade(db: Session, bridge, user_id: int | None, symbol: str | None = None, direction: str = "BUY") -> dict:
    """One real DEMO order through the normal execution path, then closed again. Returns every step."""
    from app.execution import manager as em
    steps: list[dict] = []
    t0 = time.perf_counter()

    def step(name: str, ok: bool, detail: str) -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail[:500],
                      "ms": round((time.perf_counter() - t0) * 1000)})
        return ok

    others: list[str] = []

    def result(ok: bool, fix: str | None = None, **extra) -> dict:
        return {"ok": ok, "steps": steps, "fix": fix, "other_problems": others, **extra}

    st = tr.get_state(db)
    if not step("DEMO mode", st.active_mode == "DEMO", f"trading mode {st.active_mode}"):
        return result(False, "The test trade runs only in DEMO. It is never placed on a LIVE account.")
    acct = tr.active_account(db)
    if not step("DEMO account active", acct is not None and acct.mode == "DEMO",
                f"account {acct.login if acct else 'none'}"):
        return result(False, "Settings → Accounts: add and verify the DEMO account, then activate it.")
    check = run(db, bridge, live=True)
    # The order path needs the database, the MT5 connector and market data. Other failing systems (for example the
    # monitor or analysis service) do not stop a test order; they are reported with the result.
    blocking = [p for s in check["systems"] if s["key"] in ("database", "mt5", "market_data")
                for p in s["problems"] if p["severity"] in ("critical", "error")]
    others = [f"{s['title']}: {p['title']}" for s in check["systems"] if s["key"] not in ("database", "mt5", "market_data")
              for p in s["problems"] if p["severity"] in ("critical", "error")]
    if not step("Pre-flight system check", not blocking,
                "database, MT5 connector and market data are working" if not blocking
                else "; ".join(p["title"] for p in blocking[:4])):
        return result(False, blocking[0]["fix"], system_check=check)
    sym = pick_symbol(db, symbol)
    if not step("Test symbol", sym is not None, sym.broker_symbol if sym else f"{symbol or 'no'} tradable symbol"):
        return result(False, "Broker Symbols → refresh, enable a forex symbol, or name a tradable symbol.")
    try:
        spec = tr.spec_for(sym, bridge.symbol(sym.broker_symbol))
        tick = bridge.tick(sym.broker_symbol)
        bid, ask = float(tick["bid"]), float(tick["ask"])
    except Exception as exc:  # noqa: BLE001
        step("Live quote", False, f"{type(exc).__name__}: {exc}")
        return result(False, "The market may be closed; otherwise check MT5 Market Watch and the connector.")
    step("Live quote", True, f"bid {bid} ask {ask}")
    # Stop loss: well beyond the spread and the broker's minimum stop distance, so the Risk Engine's stop checks pass.
    dist = max((ask - bid) * 20, spec.stops_level * spec.point * 3, spec.point * 200)
    entry = ask if direction == "BUY" else bid
    sl = round(entry - dist if direction == "BUY" else entry + dist, spec.digits)
    rr = max(2.0, float(tr.account_risk_params(db, acct).min_rr or 0) + 0.5)        # meets the minimum reward:risk
    tp = round(entry + dist * rr if direction == "BUY" else entry - dist * rr, spec.digits)
    key = f"{acct.id}:test-trade:{user_id or 0}:{uuid.uuid4().hex[:12]}"
    try:
        order = em.open_position(db, bridge, symbol=sym.broker_symbol, direction=direction, stop_loss=sl,
                                 take_profit=tp, source=TEST_SOURCE, user_id=user_id, dedupe_key=key,
                                 requested_volume=float(spec.volume_min), account=acct)
    except em.ExecutionBlocked as exc:
        step("Risk Engine approval", False, exc.reason)
        return result(False, "The Risk Engine or a trading control refused the order (reasons above). Fix that "
                             "control (Control Center / Risk settings); the test never bypasses it.")
    step("Risk Engine approval", True, f"approved {order.volume} lots, SL {sl}, TP {tp}")
    if not step("Order filled by the broker", order.status == "CONFIRMED",
                f"{order.action} {order.symbol} {order.volume}: {order.status} {order.status_detail or ''}"):
        return result(False, "The broker did not confirm the order: see the detail (market closed, trading disabled "
                             "for the symbol, not enough margin) and the MT5 journal.", order_id=order.id)
    pos = db.scalar(select(OpenPosition).where(OpenPosition.execution_id == order.execution_id))
    ticket = int(pos.ticket) if pos is not None else (int(order.position_ticket) if order.position_ticket else None)
    if not step("Position visible in the engine", pos is not None, f"ticket {ticket}" if pos is not None
                else "the confirmed order has no open position in the engine"):
        return result(False, "The fill was confirmed but the position is not visible; run reconciliation "
                             "(Trading Console) and check MT5.", order_id=order.id)
    try:
        closed = em.manage(db, bridge, action="CLOSE", position_ticket=ticket, dedupe_key=f"{key}:close",
                           user_id=user_id, source=TEST_SOURCE, account=acct)
        step("Position closed", closed.status == "CONFIRMED", f"close: {closed.status} {closed.status_detail or ''}")
    except Exception as exc:  # noqa: BLE001
        step("Position closed", False, f"{type(exc).__name__}: {exc}")
        return result(False, f"Close the test position (ticket {ticket}) in the Trading Console or MT5. It has a stop "
                             "loss.", order_id=order.id)
    tr.sync_positions(db, bridge, acct)
    still = db.scalar(select(OpenPosition).where(OpenPosition.account_id == acct.id, OpenPosition.ticket == ticket))
    if not step("Close confirmed by the broker", still is None, "position no longer open" if still is None
                else "position still listed as open"):
        return result(False, f"Check ticket {ticket} in MT5 and close it if it is still open.", order_id=order.id)
    db.commit()
    return result(True, None, order_id=order.id, symbol=sym.broker_symbol, volume=float(order.volume),
                  ticket=ticket, seconds=round(time.perf_counter() - t0, 1))
