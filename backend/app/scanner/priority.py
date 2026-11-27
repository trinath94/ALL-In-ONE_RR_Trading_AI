"""Priority symbols: the owner's high-volatility list, scanned first and ranked first every day.

* Names are matched to the broker's symbols by their canonical name, so XAUUSD finds XAUUSD.r, XAUUSDm or XAUUSD.pro;
  US30 finds US30.cash. A list can hold several names for the same market (US100 / USTEC / NAS100): whichever the
  broker offers is used.
* A matched symbol gets scan priority 10 (others keep 100) and is switched on for scanning when the broker allows
  trading it and the owner has not excluded it. Removing a name from the list puts its priority back to 100; scanning
  stays as the owner left it.
* Shortlisted candidates on priority symbols are ranked before the others; their grade and score are not changed.
* Priority never loosens a risk limit. High-volatility symbols usually have wider spreads than the Risk Engine's global
  spread limit; ``spread_view`` shows the latest spread against the limit so the owner can set a limit per symbol.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

PRIORITY = 10
NORMAL = 100
MAX_SUFFIX = 3          # XAUUSDM, XAUUSDPRO: broker suffixes after the canonical name

DEFAULT = ["XAUUSD", "XAGUSD", "USOIL", "WTI", "XTIUSD", "UKOIL", "BRENT", "XBRUSD", "GBPJPY", "AUDJPY", "NZDJPY",
           "CADJPY", "EURJPY", "GBPAUD", "GBPCAD", "EURNZD", "USDZAR", "USDMXN", "USDTRY", "NAS100", "USTEC", "US100",
           "US30", "DJ30", "WS30", "US500", "SPX500", "GER40", "DE40", "DAX", "UK100", "JP225", "BTCUSD", "ETHUSD",
           "SOLUSD", "XRPUSD"]


def clean(names) -> list[str]:
    out: list[str] = []
    for n in names or []:
        c = "".join(ch for ch in str(n) if ch.isalnum()).upper()[:16]
        if c and c not in out:
            out.append(c)
    return out


def listed(db: Session) -> list[str]:
    from app.db.models import ScannerSettings
    row = db.get(ScannerSettings, 1)
    return clean(row.priority_symbols) if row is not None and row.priority_symbols is not None else list(DEFAULT)


def match(canonical: str, names: list[str]) -> str | None:
    c = (canonical or "").upper()
    for n in names:
        if c == n or (c.startswith(n) and len(c) - len(n) <= MAX_SUFFIX):
            return n
    return None


def apply(db: Session) -> dict:
    """Bring symbol priority and scanning in line with the list. Only rows that change are written."""
    from app.db.models import Symbol
    names = listed(db)
    found: dict[str, list[str]] = {}
    enabled = []
    for s in db.scalars(select(Symbol).where(Symbol.missing_since.is_(None))):
        n = match(s.canonical, names)
        if n:
            found.setdefault(n, []).append(s.broker_symbol)
            if s.priority != PRIORITY:
                s.priority = PRIORITY
            if s.tradable and not s.excluded and not s.scan_enabled:
                s.scan_enabled = True
                enabled.append(s.broker_symbol)
        elif s.priority == PRIORITY:
            s.priority = NORMAL
    return {"list": names, "found": found, "not_at_broker": [n for n in names if n not in found],
            "scan_enabled_now": enabled}


def spread_view(db: Session) -> list[dict]:
    """Each priority symbol at the broker: scanning state, the latest spread the scanner measured and the limit that
    applies (the symbol's own limit, else the global one)."""
    from app.db.models import AnalysisResult, Symbol
    from app.services import trading as tr
    names = listed(db)
    acct = tr.active_account(db)
    params = tr.account_risk_params(db, acct)
    rows = []
    for s in db.scalars(select(Symbol).where(Symbol.missing_since.is_(None)).order_by(Symbol.canonical)):
        n = match(s.canonical, names)
        if not n:
            continue
        sp = db.scalar(select(AnalysisResult.structure["spread_points"].astext)
                       .where(AnalysisResult.symbol_id == s.id).order_by(AnalysisResult.id.desc()).limit(1))
        spread = float(sp) if sp not in (None, "null") else None
        own = (params.symbol_max_spread_points or {}).get(s.broker_symbol)
        limit = float(own) if own else params.max_spread_points
        rows.append({"name": n, "symbol": s.broker_symbol, "category": s.category, "tradable": s.tradable,
                     "excluded": s.excluded, "scanning": s.scan_enabled, "priority": s.priority,
                     "spread_points": spread, "spread_limit": limit, "own_limit": float(own) if own else None,
                     "blocked_by_spread": spread is not None and spread > limit})
    return rows
