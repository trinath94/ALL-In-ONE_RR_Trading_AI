"""Agent control room and AI provider gateway API (RR Trading One Phase 9).

Reading needs a logged-in user. Changing providers, keys and routes needs `system.manage` and is audited.
API keys are write-only: they are encrypted on arrival (RR_AI_KEYS_KEY) and never returned, logged or audited.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import gateway
from app.ai.providers import KINDS
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
    last_ids = dict(db.execute(select(AgentRun.agent_id, func.max(AgentRun.id)).group_by(AgentRun.agent_id)).all())
    last = {r.agent_id: r for r in db.scalars(select(AgentRun).where(AgentRun.id.in_(list(last_ids.values()) or [0])))}
    out = []
    for a in REGISTRY:
        lr = last.get(a.id)
        out.append({**a.dict(), "runs_24h": {st: counts.get((a.id, st), 0) for st in ("ok", "partial", "error", "skipped")},
                    "last_run": _run(lr) if lr else None})
    return out


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
            "updated_at": p.updated_at}


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


class ProviderPatch(BaseModel):
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)     # "" removes the stored key
    enabled: bool | None = None
    timeout_s: int | None = Field(default=None, ge=5, le=600)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    price_in_per_mtok: float | None = Field(default=None, ge=0)
    price_out_per_mtok: float | None = Field(default=None, ge=0)


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


@router.get("/ai/providers")
def providers(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_provider(p) for p in db.scalars(select(AIProvider).order_by(AIProvider.id))]


@router.post("/ai/providers")
def create_provider(body: ProviderIn, user: User = Depends(require("system.manage")), db: Session = Depends(get_db)):
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}")
    if db.scalar(select(AIProvider).where(AIProvider.name == body.name)):
        raise HTTPException(409, "a provider with this name exists")
    p = AIProvider(name=body.name, kind=body.kind, base_url=_check_url(body.kind, body.base_url),
                   api_key_enc=_encrypt(body.api_key) if body.api_key else None, enabled=body.enabled,
                   timeout_s=body.timeout_s, max_retries=body.max_retries, price_in_per_mtok=body.price_in_per_mtok,
                   price_out_per_mtok=body.price_out_per_mtok, updated_by=user.id)
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
    r = db.get(AITaskRoute, task)
    if not body.chain:
        if r:
            db.delete(r)
    else:
        r = r or AITaskRoute(task=task, chain=[])
        r.chain, r.updated_by = [h.model_dump() for h in body.chain], user.id
        db.add(r)
    audit(db, user.id, "AI_ROUTE_SET", task, {"chain": [h.model_dump() for h in body.chain]})
    return {"task": task, "chain": [h.model_dump() for h in body.chain]}


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
