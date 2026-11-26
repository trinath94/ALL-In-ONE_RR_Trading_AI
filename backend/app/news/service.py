"""Economic calendar & news-risk windows.

Provider integration is a small adapter: configure RR_NEWS_PROVIDER=generic_json and
RR_NEWS_API_URL pointing at a calendar endpoint returning a JSON list of
``{"id","title","currency","impact","time"(ISO-8601 UTC),"forecast","previous","actual"}``.
Other vendors can be added by implementing ``fetch_events``.

If no provider is configured, or the last successful fetch is older than ``STALE_AFTER``,
status is UNKNOWN — the system never assumes there is no news risk.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import EconomicEvent, Symbol
from app.services.events import get_redis

log = logging.getLogger("news")
UTC = timezone.utc
STALE_AFTER = timedelta(hours=6)
CENTRAL_BANK_WORDS = ("rate decision", "interest rate", "fomc", "ecb", "boe", "boj", "rba", "snb", "boc", "rbnz",
                      "cpi", "nonfarm", "non-farm", "payroll", "gdp", "unemployment")

# Indices/commodities -> currencies whose news moves them
INSTRUMENT_CCY = {"US30": ["USD"], "NAS100": ["USD"], "SPX500": ["USD"], "US500": ["USD"], "GER40": ["EUR"],
                  "DE40": ["EUR"], "UK100": ["GBP"], "JP225": ["JPY"], "XAU": ["USD"], "XAG": ["USD"],
                  "BTC": ["USD"], "ETH": ["USD"], "WTI": ["USD"], "BRENT": ["USD"], "USOIL": ["USD"]}


# Built-in economic calendar (no key): the public weekly calendar feed, refreshed hourly by the monitor role.
FAIRECONOMY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "holiday": "LOW", "non-economic": "LOW"}


def parse_faireconomy(items) -> list[dict]:
    """Calendar entries ``{title, country, date (ISO-8601 with offset), impact, forecast, previous}`` -> the engine's
    event format. Entries without a currency or a parsable time are skipped, never guessed."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        cur, raw = str(it.get("country") or "").strip().upper(), str(it.get("date") or "").strip()
        if not cur or not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        when = when.astimezone(UTC)
        title = str(it.get("title") or "")[:255]
        out.append({"id": f"ff-{cur}-{when.isoformat()}-{title}"[:128], "title": title, "currency": cur,
                    "impact": IMPACT.get(str(it.get("impact") or "").strip().lower(), "LOW"),
                    "time": when.isoformat(), "forecast": it.get("forecast") or None,
                    "previous": it.get("previous") or None, "actual": it.get("actual") or None})
    return out


def fetch_events() -> list[dict]:
    cfg = get_settings()
    if cfg.news_provider == "faireconomy":
        r = httpx.get(cfg.news_api_url or FAIRECONOMY_URL, timeout=15, headers={"User-Agent": "RR-Trading-One/1.0"})
        r.raise_for_status()
        events = parse_faireconomy(r.json())
        if not events:
            raise RuntimeError("economic calendar returned no usable events")
        return events
    if cfg.news_provider != "generic_json" or not cfg.news_api_url:
        raise RuntimeError("news provider not configured")
    headers = {"Authorization": f"Bearer {cfg.news_api_key}"} if cfg.news_api_key else {}
    r = httpx.get(cfg.news_api_url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def refresh(db: Session) -> dict:
    from app.services import sources
    try:
        items = fetch_events()
    except Exception as exc:  # noqa: BLE001
        log.warning("news refresh failed: %s", exc)
        if "not configured" not in str(exc):
            sources.observe("economic_calendar", False, str(exc)[:300])
        return {"ok": False, "error": str(exc)}
    sources.observe("economic_calendar", True)
    n = 0
    for it in items:
        pid = str(it.get("id") or f"{it['currency']}-{it['time']}-{it['title']}")[:128]
        prov = (get_settings().news_provider or "generic_json")[:32]
        ev = db.scalar(select(EconomicEvent).where(EconomicEvent.provider == prov, EconomicEvent.provider_id == pid))
        if ev is None:
            ev = EconomicEvent(provider=prov, provider_id=pid)
            db.add(ev)
        ev.title = it["title"][:255]
        ev.currency = it["currency"].upper()[:8]
        ev.impact = str(it.get("impact", "LOW")).upper()[:8]
        ev.scheduled_at = datetime.fromisoformat(it["time"].replace("Z", "+00:00"))
        ev.forecast, ev.previous, ev.actual = it.get("forecast"), it.get("previous"), it.get("actual")
        ev.fetched_at = datetime.now(UTC)
        n += 1
    try:
        get_redis().set("rr:news:last_ok", datetime.now(UTC).isoformat())
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "events": n}


def provider_healthy() -> bool:
    if not get_settings().news_provider:
        return False
    try:
        v = get_redis().get("rr:news:last_ok")
    except Exception:  # noqa: BLE001
        return False
    return bool(v) and datetime.now(UTC) - datetime.fromisoformat(v) < STALE_AFTER


def symbol_currencies(sym: Symbol | None, name: str) -> list[str]:
    out = set()
    if sym is not None:
        out |= {c for c in (sym.base_currency, sym.profit_currency) if c}
    up = name.upper()
    for k, v in INSTRUMENT_CCY.items():
        if k in up:
            out |= set(v)
    return sorted(out)


def is_high_impact(ev: EconomicEvent) -> bool:
    return ev.impact == "HIGH" or any(w in ev.title.lower() for w in CENTRAL_BANK_WORDS)


def status_for(db: Session, sym: Symbol | None, name: str, now: datetime | None = None) -> tuple[str, list[EconomicEvent]]:
    """CLEAR | RESTRICTED | UNKNOWN for a symbol at `now`."""
    if not provider_healthy():
        return "UNKNOWN", []
    cfg = get_settings()
    now = now or datetime.now(UTC)
    ccys = symbol_currencies(sym, name)
    win_b, win_a = timedelta(minutes=cfg.news_block_minutes_before), timedelta(minutes=cfg.news_block_minutes_after)
    evs = db.scalars(select(EconomicEvent).where(EconomicEvent.currency.in_(ccys),
                                                 EconomicEvent.scheduled_at.between(now - win_a, now + win_b))).all()
    hits = [e for e in evs if is_high_impact(e)]
    return ("RESTRICTED" if hits else "CLEAR"), hits
