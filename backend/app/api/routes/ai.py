"""Agent control room and AI provider gateway API (RR Trading One Phase 9).

Reading needs a logged-in user. Changing providers, keys and routes needs `system.manage` and is audited.
API keys are write-only: they are encrypted on arrival (RR_AI_KEYS_KEY) and never returned, logged or audited.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import gateway
from app.ai.providers import KINDS, PRESETS, ProviderError
from app.api.deps import current_user, require
from app.db.models import User
from app.db.models_ai import AgentRun, AIProvider, AITaskRoute, AIUsage
from app.db.session import get_db
from app.orchestration.agents import BY_ID, REGISTRY
from app.services.events import audit

router = APIRouter(prefix="/api", tags=["agents-ai"])


def _run(r: AgentRun) -> dict:
    return {"id": r.id, "cycle_id": r.cycle_id, "agent_id": r.agent_id, "agent_version": r.agent_version, "task": r.task,
            "input_refs": r.input_refs, "output_schema": r.output_schema, "output": r.output, "status": r.status,
            "started_at": r.started_at, "completed_at": r.completed_at, "duration_ms": r.duration_ms,
            "provider": r.provider, "model": r.model, "evidence": r.evidence, "errors": r.errors}


# ---- agents --------------------------------------------------------------------------------------------------------
@router.get("/agents")
def agents(_: User = Depends(current_user), db: Session = Depends(get_db)):
    """The 21 agents, what each is, and what each actually did (from the run log only)."""
    since = datetime.now(UTC) - timedelta(hours=24)
    counts = {(a, st): n for a, st, n in db.execute(
        select(AgentRun.agent_id, AgentRun.status, func.count()).where(AgentRun.created_at >= since)
        .group_by(AgentRun.agent_id, AgentRun.status))}
    # One index lookup per registered agent (Phase 20): a GROUP BY over the whole run log grew with its history.
    last_ids = {a.id: i for a in REGISTRY
                if (i := db.scalar(select(func.max(AgentRun.id)).where(AgentRun.agent_id == a.id))) is not None}
    last = {r.agent_id: r for r in db.scalars(select(AgentRun).where(AgentRun.id.in_(list(last_ids.values()) or [0])))}
    out = []
    for a in REGISTRY:
        lr = last.get(a.id)
        r24 = {st: counts.get((a.id, st), 0) for st in ("ok", "partial", "error", "skipped")}
        out.append({**a.dict(), "runs_24h": r24, "last_run": _run(lr) if lr else None,
                    # Phase 17 (Part 17 Control Room): health from the last 24 h of runs; the markets of the last run
                    "health": _health(r24), "markets": _markets(lr)})
    return out


def _health(r24: dict) -> str:
    n = sum(r24.values())
    if n == 0:
        return "idle (no run in 24 h)"
    if r24["error"] and not (r24["ok"] or r24["partial"]):
        return "failing"
    return "degraded" if r24["error"] else "healthy"


def _markets(lr: AgentRun | None) -> list[str]:
    """Symbol:timeframe references of the agent's last run (e.g. EURUSD.r:H1), de-duplicated."""
    if lr is None:
        return []
    refs = (lr.input_refs or {}).get("refs") or []
    seen = []
    for ref in refs:
        parts = str(ref).split(":")
        if len(parts) >= 2 and parts[1] in ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"):
            m = f"{parts[0]} {parts[1]}"
            if m not in seen:
                seen.append(m)
    return seen


@router.get("/agents/runs")
def agent_runs(_: User = Depends(current_user), db: Session = Depends(get_db), agent_id: str | None = None,
               cycle_id: str | None = None, limit: int = 100):
    if agent_id and agent_id not in BY_ID:
        raise HTTPException(404, "unknown agent")
    q = select(AgentRun).order_by(AgentRun.id.desc()).limit(min(max(limit, 1), 500))
    if agent_id:
        q = q.where(AgentRun.agent_id == agent_id)
    if cycle_id:
        q = q.where(AgentRun.cycle_id == cycle_id)
    return [_run(r) for r in db.scalars(q)]


@router.get("/agents/cycles")
def agent_cycles(_: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 30):
    q = select(AgentRun).where(AgentRun.agent_id == "master_orchestrator").order_by(AgentRun.id.desc()) \
        .limit(min(max(limit, 1), 200))
    return [_run(r) for r in db.scalars(q)]


# ---- providers -----------------------------------------------------------------------------------------------------
def _provider(p: AIProvider) -> dict:
    return {"id": p.id, "name": p.name, "kind": p.kind, "base_url": p.base_url, "has_key": bool(p.api_key_enc),
            "enabled": p.enabled, "timeout_s": p.timeout_s, "max_retries": p.max_retries,
            "price_in_per_mtok": p.price_in_per_mtok, "price_out_per_mtok": p.price_out_per_mtok,
            "max_requests_per_minute": p.max_requests_per_minute, "updated_at": p.updated_at}


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9 ._()-]+$")
    kind: str
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    timeout_s: int = Field(default=120, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    price_in_per_mtok: float | None = Field(default=None, ge=0)
    price_out_per_mtok: float | None = Field(default=None, ge=0)
    max_requests_per_minute: int | None = Field(default=None, ge=1, le=10000)


class ProviderPatch(BaseModel):
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)     # "" removes the stored key
    enabled: bool | None = None
    timeout_s: int | None = Field(default=None, ge=5, le=600)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    price_in_per_mtok: float | None = Field(default=None, ge=0)
    price_out_per_mtok: float | None = Field(default=None, ge=0)
    max_requests_per_minute: int | None = Field(default=None, ge=1, le=10000)


def _check_url(kind: str, url: str | None) -> str | None:
    if kind == "openai_compatible":
        if not url or not url.startswith(("https://", "http://")):
            raise HTTPException(422, "an OpenAI-compatible provider needs a base URL (https://..., or http:// for a "
                                     "local server such as Ollama)")
    return url.rstrip("/") if url else None


def _encrypt(key: str) -> str:
    from app.core.security import encrypt_ai_key
    try:
        return encrypt_ai_key(key)
    except RuntimeError as exc:
        raise HTTPException(409, f"{exc}") from None


def _last_calls(db: Session) -> dict:
    """Per provider name: the last successful request and the last failed one, from the usage ledger."""
    out: dict = {}
    ok = db.execute(select(AIUsage.provider, func.max(AIUsage.created_at)).where(AIUsage.status == "ok")
                    .group_by(AIUsage.provider)).all()
    for name, at in ok:
        out.setdefault(name, {})["last_success_at"] = at
    bad = db.execute(select(AIUsage.provider, func.max(AIUsage.id)).where(AIUsage.status.notin_(["ok", "skipped"]))
                     .group_by(AIUsage.provider)).all()
    for name, uid in bad:
        u = db.get(AIUsage, uid)
        out.setdefault(name, {}).update({"last_error_at": u.created_at, "last_error": (u.error or u.status)[:300]})
    return out


@router.get("/ai/providers")
def providers(_: User = Depends(current_user), db: Session = Depends(get_db)):
    last = _last_calls(db)
    return [{**_provider(p), "last_success_at": None, "last_error_at": None, "last_error": None, **last.get(p.name, {})}
            for p in db.scalars(select(AIProvider).order_by(AIProvider.id))]


# ---- Single Active AI ----------------------------------------------------------------------------------------------
CLAUDE_PRO = {"available": False, "label": "Claude Pro / Claude.ai subscription",
              "reason": "Anthropic offers no official way for an application to use a Claude.ai (Pro or Max) "
                        "subscription; the app does not use browser sessions or cookies. Use the Anthropic API "
                        "(an API key from the Anthropic Console, billed separately) as the Claude provider."}


def _control(db: Session) -> dict:
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1) or AIControl(id=1, single_active=False, active_model="", fallback_enabled=False,
                                           fallback_model="")
    name = lambda pid: (db.get(AIProvider, pid).name if pid and db.get(AIProvider, pid) else None)  # noqa: E731
    hops = gateway.single_active_chain(db)
    return {"single_active": bool(c.single_active), "active_provider_id": c.active_provider_id,
            "active_provider": name(c.active_provider_id), "active_model": c.active_model or "",
            "fallback_enabled": bool(c.fallback_enabled), "fallback_provider_id": c.fallback_provider_id,
            "fallback_provider": name(c.fallback_provider_id), "fallback_model": c.fallback_model or "",
            "effective_chain": [{"provider": h.name, "model": h.model} for h in hops] if hops is not None else None,
            "updated_at": c.updated_at, "claude_pro": CLAUDE_PRO}


@router.get("/ai/control")
def get_control(_: User = Depends(current_user), db: Session = Depends(get_db)):
    """Single Active AI: whether one provider serves every AI task, which one, and its optional fallback.
    ``effective_chain`` is what the gateway uses now (None: the per-task routes apply)."""
    return _control(db)


class ControlIn(BaseModel):
    single_active: bool
    active_provider_id: int | None = None
    active_model: str = Field(default="", max_length=96)
    fallback_enabled: bool = False
    fallback_provider_id: int | None = None
    fallback_model: str = Field(default="", max_length=96)


@router.put("/ai/control")
def put_control(body: ControlIn, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    """Switch Single Active AI on or off, choose the provider and model, and an optional fallback. The fallback is
    used only when enabled here; the gateway never switches to any other provider. Switching providers changes
    nothing in the trading engine; every analysis records the provider and model that answered."""
    from app.db.models_ai import AIControl
    if body.single_active:
        p = db.get(AIProvider, body.active_provider_id) if body.active_provider_id else None
        if p is None:
            raise HTTPException(422, "choose the active provider")
        if not p.enabled:
            raise HTTPException(409, f"{p.name} is switched off; switch it on first")
        if not body.active_model.strip():
            raise HTTPException(422, "choose the active provider's model")
        if body.fallback_enabled:
            fb = db.get(AIProvider, body.fallback_provider_id) if body.fallback_provider_id else None
            if fb is None or fb.id == p.id:
                raise HTTPException(422, "choose a fallback provider different from the active one")
            if not body.fallback_model.strip():
                raise HTTPException(422, "choose the fallback provider's model")
    c = db.get(AIControl, 1)
    if c is None:
        c = AIControl(id=1)
        db.add(c)
    old = _control(db) if c.updated_at else None
    c.single_active = body.single_active
    if body.single_active or body.active_provider_id is not None:
        c.active_provider_id, c.active_model = body.active_provider_id, body.active_model.strip()
    c.fallback_enabled = body.fallback_enabled and body.single_active
    c.fallback_provider_id = body.fallback_provider_id if c.fallback_enabled else None
    c.fallback_model = body.fallback_model.strip() if c.fallback_enabled else ""
    c.updated_by, c.updated_at = user.id, datetime.now(UTC)
    db.flush()
    new = _control(db)
    keep = ("single_active", "active_provider", "active_model", "fallback_enabled", "fallback_provider", "fallback_model")
    audit(db, user.id, "AI_CONTROL_UPDATE", new["active_provider"] or "",
          {"old": {k: old[k] for k in keep} if old else None, "new": {k: new[k] for k in keep}})
    return new


@router.post("/ai/providers")
def create_provider(body: ProviderIn, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}")
    if db.scalar(select(AIProvider).where(AIProvider.name == body.name)):
        raise HTTPException(409, "a provider with this name exists")
    p = AIProvider(name=body.name, kind=body.kind, base_url=_check_url(body.kind, body.base_url),
                   api_key_enc=_encrypt(body.api_key) if body.api_key else None, enabled=body.enabled,
                   timeout_s=body.timeout_s, max_retries=body.max_retries, price_in_per_mtok=body.price_in_per_mtok,
                   price_out_per_mtok=body.price_out_per_mtok, max_requests_per_minute=body.max_requests_per_minute,
                   updated_by=user.id)
    db.add(p)
    db.flush()
    audit(db, user.id, "AI_PROVIDER_CREATE", p.name, {**body.model_dump(exclude={"api_key"}), "key_set": bool(body.api_key)})
    return _provider(p)


@router.patch("/ai/providers/{provider_id}")
def patch_provider(provider_id: int, body: ProviderPatch, user: User = Depends(require("system.manage")),
                   db: Session = Depends(get_db)):
    p = db.get(AIProvider, provider_id)
    if p is None:
        raise HTTPException(404)
    changes = body.model_dump(exclude_unset=True)
    key = changes.pop("api_key", None)
    if "base_url" in changes:
        changes["base_url"] = _check_url(p.kind, changes["base_url"])
    for k, v in changes.items():
        setattr(p, k, v)
    if key is not None:
        p.api_key_enc = _encrypt(key) if key else None
    p.updated_by = user.id
    audit(db, user.id, "AI_PROVIDER_UPDATE", p.name, {**changes, **({"key": "replaced" if key else "removed"}
                                                                     if key is not None else {})})
    return _provider(p)


# ---- task routes ---------------------------------------------------------------------------------------------------
class Hop(BaseModel):
    provider_id: int
    model: str = Field(min_length=1, max_length=96)


class RouteIn(BaseModel):
    chain: list[Hop] = Field(max_length=4)        # [] removes the route (original behaviour: RR_ANTHROPIC_API_KEY)
    # Parts 27-30: "consensus" (candidate review only) asks the first two providers independently; both must confirm.
    mode: Literal["fallback", "consensus"] = "fallback"


@router.get("/ai/routes")
def routes(_: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = {r.task: r for r in db.scalars(select(AITaskRoute))}
    names = {p.id: p.name for p in db.scalars(select(AIProvider))}
    out = []
    for task, label in gateway.TASKS.items():
        r = rows.get(task)
        chain = [{**h, "provider": names.get(h["provider_id"], "?")} for h in (r.chain if r else [])]
        hops = gateway.chain_for(task)
        out.append({"task": task, "label": label, "used_by_engine": task in gateway.WIRED_TASKS, "chain": chain,
                    "mode": (r.mode if r and r.chain else "fallback"),
                    "consensus_supported": task in gateway.CONSENSUS_TASKS,
                    "effective": [{"provider": h.name, "model": h.model} for h in hops],
                    "source": "route" if r and r.chain else ("RR_ANTHROPIC_API_KEY" if hops else "not configured")})
    return out


@router.put("/ai/routes/{task}")
def set_route(task: str, body: RouteIn, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    if task not in gateway.TASKS:
        raise HTTPException(404, "unknown task")
    for h in body.chain:
        if db.get(AIProvider, h.provider_id) is None:
            raise HTTPException(422, f"provider {h.provider_id} does not exist")
    if body.mode == "consensus":
        if task not in gateway.CONSENSUS_TASKS:
            raise HTTPException(422, f"consensus mode is available for {', '.join(gateway.CONSENSUS_TASKS)} only")
        if len({h.provider_id for h in body.chain[:2]}) < 2:
            raise HTTPException(422, "consensus mode needs two different providers first in the chain")
    r = db.get(AITaskRoute, task)
    if not body.chain:
        if r:
            db.delete(r)
    else:
        r = r or AITaskRoute(task=task, chain=[])
        r.chain, r.mode, r.updated_by = [h.model_dump() for h in body.chain], body.mode, user.id
        db.add(r)
    audit(db, user.id, "AI_ROUTE_SET", task, {"chain": [h.model_dump() for h in body.chain], "mode": body.mode})
    return {"task": task, "chain": [h.model_dump() for h in body.chain], "mode": body.mode}


# ---- usage, health, test ---------------------------------------------------------------------------------------------
@router.get("/ai/usage")
def usage(_: User = Depends(current_user), db: Session = Depends(get_db), days: int = 7):
    since = datetime.now(UTC) - timedelta(days=min(max(days, 1), 90))
    rows = db.execute(select(AIUsage.provider, AIUsage.model, AIUsage.task, AIUsage.status, func.count(),
                             func.sum(AIUsage.input_tokens), func.sum(AIUsage.output_tokens),
                             func.sum(AIUsage.cost), func.count(AIUsage.cost), func.avg(AIUsage.latency_ms),
                             func.min(AIUsage.created_at), func.max(AIUsage.created_at))
                      .where(AIUsage.created_at >= since)
                      .group_by(AIUsage.provider, AIUsage.model, AIUsage.task, AIUsage.status)).all()
    summary = [{"provider": p, "model": m, "task": t, "status": st, "calls": n, "input_tokens": int(ti or 0),
                "output_tokens": int(to or 0), "cost": round(c, 6) if nc else None,
                "cost_note": None if nc == n else ("no price configured" if not nc else "price configured for some calls only"),
                "avg_latency_ms": round(lat or 0, 1), "first_seen": f0, "last_seen": f1}
               for p, m, t, st, n, ti, to, c, nc, lat, f0, f1 in rows]
    recent = [{"at": u.created_at, "task": u.task, "provider": u.provider, "model": u.model, "status": u.status,
               "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cost": u.cost,
               "latency_ms": u.latency_ms, "attempts": u.attempts, "fallback_from": u.fallback_from, "error": u.error}
              for u in db.scalars(select(AIUsage).order_by(AIUsage.id.desc()).limit(50))]
    return {"days": days, "summary": summary, "recent": recent}


@router.get("/ai/health")
def health(_: User = Depends(current_user), db: Session = Depends(get_db)):
    """Per provider, from the ledger only: last success, last error, error rate in the last hour, cooldown."""
    from app.services.events import get_redis
    hour = datetime.now(UTC) - timedelta(hours=1)
    names = [p.name for p in db.scalars(select(AIProvider))] + [gateway.ENV_PROVIDER]
    out = []
    for name in names:
        ok = db.scalar(select(func.max(AIUsage.created_at)).where(AIUsage.provider == name, AIUsage.status == "ok"))
        err = db.scalar(select(AIUsage).where(AIUsage.provider == name, AIUsage.status.in_(
            ("error", "invalid_output", "rate_limited"))).order_by(AIUsage.id.desc()).limit(1))
        n = db.scalar(select(func.count()).where(AIUsage.provider == name, AIUsage.created_at >= hour)) or 0
        bad = db.scalar(select(func.count()).where(AIUsage.provider == name, AIUsage.created_at >= hour,
                                                   AIUsage.status != "ok")) or 0
        if name == gateway.ENV_PROVIDER and not (ok or err or n):
            continue
        try:
            cooling = get_redis().get(f"rr:ai:cooldown:{name}")
        except Exception:  # noqa: BLE001
            cooling = None
        state = "cooling down" if cooling else ("unknown (never called)" if not (ok or err) else
                                                ("failing" if n and bad == n else ("degraded" if bad else "healthy")))
        out.append({"provider": name, "state": state, "last_success": ok,
                    "last_error": {"at": err.created_at, "status": err.status, "error": err.error} if err else None,
                    "calls_last_hour": n, "failures_last_hour": bad, "cooldown": cooling})
    return out


class Ping(BaseModel):
    status: str
    note: str


@router.post("/ai/providers/{provider_id}/test")
def test_provider(provider_id: int, model: str, user: User = Depends(require("system.manage")),
                  db: Session = Depends(get_db)):
    """One small schema-checked call through the gateway (task 'diagnostics'), recorded in the ledger like any call."""
    p = db.get(AIProvider, provider_id)
    if p is None:
        raise HTTPException(404)
    hop = gateway.Hop(p.name, p.kind, model, p.base_url, None, float(p.timeout_s), 0, p.price_in_per_mtok,
                      p.price_out_per_mtok)
    try:
        hop.api_key = gateway._decrypt(p.api_key_enc)
    except Exception:  # noqa: BLE001
        raise HTTPException(409, "the stored key cannot be decrypted (RR_AI_KEYS_KEY changed?)") from None
    audit(db, user.id, "AI_PROVIDER_TEST", p.name, {"model": model})
    db.commit()
    schema = {"type": "object", "additionalProperties": False, "required": ["status", "note"],
              "properties": {"status": {"type": "string", "enum": ["ok"]}, "note": {"type": "string"}}}
    try:
        out, meta = gateway.run_hops("diagnostics", [hop], "You are a connectivity check. Reply with status ok and a "
                                     "one-sentence note.", {"task": "connectivity_check"}, schema, Ping, max_tokens=200)
    except gateway.GatewayError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "model": meta["model"], "latency_ms": round(meta["latency_ms"], 1),
            "input_tokens": meta["input_tokens"], "output_tokens": meta["output_tokens"]}


# ---- Phase 14: presets, model discovery, model version history ------------------------------------------------------
@router.get("/ai/presets")
def presets(_: User = Depends(current_user)):
    return PRESETS


@router.get("/ai/providers/{provider_id}/models")
def provider_models(provider_id: int, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    """The models this provider reports it offers (its own model list endpoint), to choose from per task."""
    p = db.get(AIProvider, provider_id)
    if p is None:
        raise HTTPException(404)
    try:
        key = gateway._decrypt(p.api_key_enc)
    except Exception:  # noqa: BLE001
        raise HTTPException(409, "the stored key cannot be decrypted (RR_AI_KEYS_KEY changed?)") from None
    hop = gateway.Hop(p.name, p.kind, "", p.base_url, key, float(min(p.timeout_s, 30)), 0, None, None)
    try:
        models = gateway._provider(hop, None).list_models()
    except ProviderError as exc:
        return {"ok": False, "provider": p.name, "error": str(exc), "models": []}
    return {"ok": True, "provider": p.name, "models": models}


@router.get("/ai/models/history")
def model_history(days: int = 90, _: User = Depends(current_user), db: Session = Depends(get_db)):
    """Model version history: per provider and requested model, every model version the provider actually served
    (first and last seen, calls, by task), plus the audit trail of route and provider changes."""
    from app.db.models import AuditLog
    since = datetime.now(UTC) - timedelta(days=min(max(days, 1), 365))
    rows = db.execute(select(AIUsage.provider, AIUsage.requested_model, AIUsage.model, AIUsage.task, func.count(),
                             func.min(AIUsage.created_at), func.max(AIUsage.created_at))
                      .where(AIUsage.created_at >= since, AIUsage.status.in_(("ok", "invalid_output")))
                      .group_by(AIUsage.provider, AIUsage.requested_model, AIUsage.model, AIUsage.task)).all()
    groups: dict[tuple, dict] = {}
    for prov, req, served, task, n, first, last in rows:
        g = groups.setdefault((prov, req), {"provider": prov, "requested_model": req, "served": {}})
        v = g["served"].setdefault(served, {"model": served, "calls": 0, "first_seen": first, "last_seen": last, "tasks": []})
        v["calls"] += n
        v["first_seen"], v["last_seen"] = min(v["first_seen"], first), max(v["last_seen"], last)
        v["tasks"] = sorted(set(v["tasks"]) | {task})
    out = []
    for g in groups.values():
        served = sorted(g["served"].values(), key=lambda v: v["first_seen"])
        out.append({**g, "served": served, "versions": len(served),
                    "changed": len(served) > 1, "current": served[-1]["model"] if served else None})
    changes = [{"at": a.created_at, "action": a.action, "target": a.target, "details": a.detail, "user_id": a.user_id}
               for a in db.scalars(select(AuditLog).where(AuditLog.action.in_(
                   ("AI_ROUTE_SET", "AI_PROVIDER_CREATE", "AI_PROVIDER_UPDATE")), AuditLog.created_at >= since)
                   .order_by(AuditLog.id.desc()).limit(200))]
    return {"days": days, "models": sorted(out, key=lambda g: (g["provider"], g["requested_model"] or "")),
            "configuration_changes": changes}
