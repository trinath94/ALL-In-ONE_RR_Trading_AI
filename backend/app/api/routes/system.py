"""System monitoring, audit, AI agent visibility and WebSocket stream."""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import socket
import time
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import SESSION_COOKIE, current_user, require
from app.core.config import get_settings
from app.core.security import decode_session_token
from app.db.models import (AgentConversation, AgentDecision, AuditLog, ClosedPosition, StrategyExperiment,
                           StrategyPerformance, SystemEvent, TradeExecution, TradeOrder, User, UserSession)
from app.db.session import get_db, session_scope
from app.services.events import CHANNEL, get_redis

router = APIRouter(prefix="/api", tags=["system"])
UTC = timezone.utc
WS_CLIENTS = {"count": 0}
STARTED = time.time()


def _check(fn):
    t0 = time.perf_counter()
    try:
        detail = fn()
        return {"status": "UP", "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"status": "DOWN", "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "detail": str(exc)[:300]}


def ssl_days_left(host: str, port: int = 443) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=3) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
        exp = datetime.strptime(ss.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    return (exp - datetime.now(UTC)).days


@router.get("/health")
def health_public(db: Session = Depends(get_db)):
    """Unauthenticated liveness for Docker/Nginx; reveals no internals."""
    db.execute(text("select 1"))
    get_redis().ping()
    return {"status": "ok"}


@router.get("/system/status")
def status(_: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_settings()
    r = get_redis()
    bridge = json.loads(r.get("rr:bridge:health") or "null")
    vm, disk = psutil.virtual_memory(), psutil.disk_usage("/")
    net = psutil.net_io_counters()
    last_claude = db.scalar(select(AgentConversation).order_by(AgentConversation.id.desc()).limit(1))
    from app.ai import gateway
    ai_hops = gateway.chain_for("candidate_review")
    claude_errors = db.scalar(select(func.count(AgentConversation.id)).where(
        AgentConversation.error.isnot(None), AgentConversation.created_at > func.now() - text("interval '1 hour'")))
    queue = db.scalar(select(func.count(TradeOrder.id)).where(TradeOrder.status.in_(["PENDING", "SENT", "UNKNOWN"])))
    lat = db.scalar(select(func.avg(TradeExecution.latency_ms)).where(
        TradeExecution.executed_at > func.now() - text("interval '1 day'")))
    errors = db.scalar(select(func.count(SystemEvent.id)).where(
        SystemEvent.level.in_(["error", "critical"]), SystemEvent.created_at > func.now() - text("interval '1 hour'")))
    ssl_info = None
    host = os.environ.get("RR_PUBLIC_HOST")
    if host:
        ssl_info = _check(lambda: {"days_left": ssl_days_left(host)})
    return {
        "server_time": datetime.now(UTC).isoformat(), "uptime_s": int(time.time() - STARTED),
        "host": {"cpu_pct": psutil.cpu_percent(interval=0.2), "load": os.getloadavg(), "cpu_count": psutil.cpu_count(),
                 "mem_pct": vm.percent, "mem_used_gb": round(vm.used / 1e9, 2), "mem_total_gb": round(vm.total / 1e9, 2),
                 "disk_pct": disk.percent, "disk_free_gb": round(disk.free / 1e9, 2),
                 "net_sent_mb": round(net.bytes_sent / 1e6, 1), "net_recv_mb": round(net.bytes_recv / 1e6, 1)},
        "services": {
            "postgresql": _check(lambda: db.execute(text("select version()")).scalar()[:40]),
            "redis": _check(lambda: r.info("server")["redis_version"]),
            "mt5_connector": {"status": "UP" if bridge and bridge.get("connected") is not None and not bridge.get("error") else "DOWN",
                              "detail": bridge},
            "mt5_terminal": {"status": "UP" if bridge and bridge.get("connected") else "DOWN",
                             "detail": {k: (bridge or {}).get(k) for k in ("terminal_build", "ping_last_us", "trade_allowed")}},
            "market_data": {"status": "UP" if bridge and bridge.get("connected") else "DOWN"},
            # AI review status follows the AI gateway (Phase 9): a configured route, or RR_ANTHROPIC_API_KEY.
            "claude_api": {"status": "NOT_CONFIGURED" if not ai_hops else ("DEGRADED" if claude_errors else "UP"),
                           "detail": {"model": " -> ".join(f"{h.name}/{h.model}" for h in ai_hops) or s.claude_model,
                                      "errors_1h": claude_errors,
                                      "last_call": last_claude.created_at if last_claude else None,
                                      "last_latency_ms": last_claude.latency_ms if last_claude else None}},
            "websocket": {"status": "UP", "connections": WS_CLIENTS["count"]},
            "ssl_certificate": ssl_info or {"status": "UNKNOWN", "detail": "RR_PUBLIC_HOST not set"},
        },
        "execution_queue": queue, "execution_latency_ms_24h": round(lat, 2) if lat else None, "errors_1h": errors,
    }


@router.get("/system/events")
def events(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 200):
    return [{"id": e.id, "component": e.component, "level": e.level, "message": e.message, "data": e.data,
             "at": e.created_at} for e in db.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(min(limit, 1000)))]


@router.get("/system/audit")
def audit_log(_: User = Depends(require("users.manage")), db: Session = Depends(get_db), limit: int = 200):
    return [{"id": a.id, "user_id": a.user_id, "action": a.action, "target": a.target, "detail": a.detail, "ip": a.ip,
             "at": a.created_at} for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 2000)))]


# ---- AI agent -------------------------------------------------------------------------------------
MODULES = ["market_scanner", "smc_engine", "ict_engine", "price_action_engine", "liquidity_engine", "indicator_engine",
           "volume_intelligence", "quantitative_engine", "strategy_optimisation", "risk_engine", "execution", "claude_agent"]


@router.get("/agent/status")
def agent_status(_: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_redis()
    raw = r.hgetall("rr:module_status")
    mods = {m: json.loads(raw[m]) if m in raw else {"status": "IDLE", "message": "no activity recorded", "ts": None}
            for m in MODULES}
    # strategy-family engines report through the scanner; derive from the latest analysis counts
    today = func.now() - text("interval '1 day'")
    return {
        "modules": mods,
        "claude_configured": _ai_configured(),
        "stats_24h": {
            "analyses": db.scalar(text("select count(*) from analysis_results where created_at > now() - interval '1 day'")),
            "signals": db.scalar(text("select count(*) from trading_signals where created_at > now() - interval '1 day'")),
            "approved": db.scalar(select(func.count(AgentDecision.id)).where(AgentDecision.created_at > today,
                                                                            AgentDecision.decision.in_(["BUY", "SELL"]))),
            "rejected": db.scalar(select(func.count(AgentDecision.id)).where(AgentDecision.created_at > today,
                                                                            AgentDecision.decision == "NO_TRADE")),
            "risk_rejections": db.scalar(text("select count(*) from risk_evaluations where not approved and created_at > now() - interval '1 day'")),
            "claude_tokens_in": db.scalar(select(func.coalesce(func.sum(AgentConversation.input_tokens), 0)).where(AgentConversation.created_at > today)),
            "claude_tokens_out": db.scalar(select(func.coalesce(func.sum(AgentConversation.output_tokens), 0)).where(AgentConversation.created_at > today)),
        },
    }


@router.get("/agent/activity")
def activity(_: User = Depends(current_user), limit: int = 100):
    return [json.loads(x) for x in get_redis().lrange("rr:activity", 0, min(limit, 500) - 1)]


@router.get("/agent/decisions")
def decisions(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 50):
    return [{"id": d.id, "symbol": d.symbol, "decision": d.decision, "signal_id": d.signal_id, "payload": d.payload,
             "at": d.created_at} for d in db.scalars(select(AgentDecision).order_by(AgentDecision.id.desc()).limit(min(limit, 500)))]


@router.post("/agent/discover")
def discover(user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    """Claude performance review + experimental strategy proposals (stored as RESEARCH only)."""
    from app.ai import gateway
    if not gateway.configured("strategy_discovery"):
        raise HTTPException(409, "no AI provider is configured for strategy discovery")
    from app.agents.claude_agent import DISCOVERY_SYSTEM, EXPERIMENT_SCHEMA, Discovery
    from app.orchestration.orchestrator import record
    from app.strategies import registry
    perf = [{"strategy_id": p.strategy_id, "symbol": p.symbol, "timeframe": p.timeframe, "regime": p.regime,
             "source": p.source, "trades": p.trades, "wins": p.wins, "gross_profit": p.gross_profit,
             "gross_loss": p.gross_loss, "avg_r": p.avg_r, "max_dd": p.max_drawdown}
            for p in db.scalars(select(StrategyPerformance).limit(2000))]
    closed = [{"symbol": c.symbol, "profit": c.profit, "r": c.r_multiple, "strategies": c.strategy_codes,
               "regime": c.regime, "closed": c.closed_at} for c in db.scalars(select(ClosedPosition).limit(2000))]
    reg = [{"code": m.code, "name": m.name, "category": m.category, "regimes": m.regimes, "requires": m.requires,
            "description": m.description} for m in registry.metas()]
    conv = AgentConversation(purpose="DISCOVERY", model="ai-gateway", request={"perf_rows": len(perf)})
    db.add(conv)
    db.flush()
    started = datetime.now(timezone.utc)
    try:
        disc, meta = gateway.run("strategy_discovery", DISCOVERY_SYSTEM, {
            "task": "performance_review_and_discovery",
            "performance": {"strategy_performance": perf, "closed_trades": closed}, "registry": reg},
            EXPERIMENT_SCHEMA, Discovery, max_tokens=32000)
    except gateway.GatewayError as exc:
        conv.error = str(exc)
        record("strategy_discovery", "performance_review_and_discovery", started_at=started, duration_ms=None,
               status="error", errors=[str(exc)[:300]])
        raise HTTPException(502, str(exc)) from None
    conv.response, conv.input_tokens, conv.output_tokens = disc.model_dump(), meta["input_tokens"], meta["output_tokens"]
    conv.model = f"{meta['provider']}/{meta['model']}"
    record("strategy_discovery", "performance_review_and_discovery", started_at=started,
           duration_ms=meta["latency_ms"], provider=meta["provider"], model=meta["model"],
           input_refs={"performance_rows": len(perf), "closed_trades": len(closed)},
           output={"experiments": len(disc.experiments)},
           evidence={"ai_output": "schema-validated; proposals stored as RESEARCH, isolated from production"})
    for e in disc.experiments:
        db.add(StrategyExperiment(title=e.title[:255], hypothesis=e.hypothesis, proposal=e.model_dump(), stage="RESEARCH"))
    from app.services.events import audit
    audit(db, user.id, "AGENT_DISCOVERY", "", {"experiments": len(disc.experiments)})
    return disc.model_dump()


# ---- WebSocket ------------------------------------------------------------------------------------
@router.websocket("/ws")
async def ws(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE)
    try:
        claims = decode_session_token(token or "")
        with session_scope() as db:
            sess = db.get(UserSession, claims.get("sid"))
            if not sess or sess.revoked:
                raise ValueError("revoked")
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return
    await websocket.accept()
    WS_CLIENTS["count"] += 1
    import redis.asyncio as aioredis
    r = aioredis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    ps = r.pubsub()
    await ps.subscribe(CHANNEL)
    try:
        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg:
                await websocket.send_text(msg["data"])
            else:
                await asyncio.sleep(0.05)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        WS_CLIENTS["count"] -= 1
        await ps.unsubscribe(CHANNEL)
        await r.aclose()


def _ai_configured() -> bool:
    from app.ai import gateway
    return gateway.configured("candidate_review") or gateway.configured("strategy_discovery")
