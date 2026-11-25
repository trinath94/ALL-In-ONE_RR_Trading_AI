"""Unified AI provider gateway (RR Trading One Phase 9, specification Parts 9 and 14).

One path for every AI call, whatever the provider:

1. **Route.** The task's route (console: AI Studio) lists providers and models in order. With no route, the
   original behaviour applies: Claude with RR_ANTHROPIC_API_KEY and RR_CLAUDE_MODEL, if a key is set.
2. **Redact.** Keys that look like credentials (password, secret, token, API key, login...) are removed from the
   payload before it leaves the engine, at any depth. Broker passwords and private credentials never reach a provider.
3. **Call** with the provider's timeout; retry connection errors and rate limits with backoff (honouring
   Retry-After up to a cap); a provider that keeps failing or is rate limited is cooled down and skipped.
4. **Validate** the output against the task's typed schema (extra fields refused). Output is untrusted input:
   invalid output is never repaired or partially used; the next provider is tried.
5. **Account** every attempt in the ai_usage ledger: provider, model the provider reported, tokens, cost (only when
   the user configured a price; never guessed), latency, attempts, fallback, error.

Trading boundaries are applied by the task (e.g. `review_candidate` -> `enforce_boundaries`) to the validated output,
so they are identical for every provider. No AI output can size a position, submit an order, enable LIVE or change a
permission: no schema has such fields and nothing downstream accepts them from an AI.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from app.ai.providers import AnthropicProvider, OpenAICompatibleProvider, ProviderError
from app.core.config import get_settings
from app.db.models_ai import AIProvider, AITaskRoute, AIUsage
from app.db.session import session_scope

log = logging.getLogger("ai.gateway")

TASKS = {
    "candidate_review": "Market candidate review (shortlisted scan candidates only)",
    "strategy_discovery": "Strategy discovery / performance review",
    "trade_research": "Historical trade research",
    "learning_explanation": "Learning explanations",
    "research_summary": "Research summarisation",
    "diagnostics": "Operational diagnostics and provider tests",
    "config_proposal": "Strategy configuration proposals (validated before any use)",
    "engine_assistant": "Engine Assistant chat (explains engine state; read-only)",
    # Claude Code + Anthropic API integration: analyses the chat orchestrator sends to the Anthropic API (or the
    # task's configured provider). Each has its own route, so a lighter or stronger model can be chosen per task.
    "market_summary": "Market summaries (engine facts only; no orders)",
    "trade_review": "Trade reviews of closed trades",
    "performance_analysis": "Performance analysis of strategies and accounts",
    "strategy_research": "Strategy research questions (no fabricated backtests)",
    "error_explanation": "Explanations of engine errors and incidents",
}
# Tasks the engine calls (Phase 14: all of them).
WIRED_TASKS = ("candidate_review", "strategy_discovery", "trade_research", "learning_explanation", "research_summary",
               "diagnostics", "config_proposal", "engine_assistant", "market_summary", "trade_review",
               "performance_analysis", "strategy_research", "error_explanation")
# Parts 27-30 Phase 4: tasks whose route may use mode "consensus" (two providers answer independently).
CONSENSUS_TASKS = ("candidate_review",)
ENV_PROVIDER = "anthropic (RR_ANTHROPIC_API_KEY)"
MAX_RETRY_AFTER = 30.0
COOLDOWN_S = 120
FAILS_BEFORE_COOLDOWN = 3
SENSITIVE = re.compile(r"pass(word|phrase)?|secret|token|api[_-]?key|credential|private[_-]?key|auth|login|cookie",
                       re.IGNORECASE)


class GatewayError(Exception):
    pass


def redact(value):
    """Drop credential-like keys at any depth. Values are kept as-is otherwise."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not (isinstance(k, str) and SENSITIVE.search(k))}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def strict(model_cls: type[BaseModel]) -> type[BaseModel]:
    """Same schema, but refusing fields the schema does not define."""
    return type(f"Strict{model_cls.__name__}", (model_cls,), {"model_config": ConfigDict(extra="forbid")})


@dataclass
class Hop:
    name: str
    kind: str
    model: str
    base_url: str | None
    api_key: str | None
    timeout_s: float
    max_retries: int
    price_in: float | None
    price_out: float | None
    rpm: int | None = None          # Phase 14: optional client-side requests-per-minute limit


def _decrypt(enc: str | None) -> str | None:
    if not enc:
        return None
    from app.core.security import decrypt_ai_key
    return decrypt_ai_key(enc)


def _hop(p: AIProvider, model: str) -> Hop | None:
    if p is None or not p.enabled:
        return None
    try:
        key = _decrypt(p.api_key_enc)
    except Exception as exc:  # noqa: BLE001 - an unreadable key skips the provider, visibly
        log.warning("provider %s: stored key unreadable (%s)", p.name, type(exc).__name__)
        return None
    return Hop(p.name, p.kind, model or "", p.base_url, key, float(p.timeout_s), int(p.max_retries),
               p.price_in_per_mtok, p.price_out_per_mtok, p.max_requests_per_minute)


def single_active_chain(db) -> list[Hop] | None:
    """Single Active AI: None when the mode is off (per-task routes apply). When on: the selected provider, then the
    explicitly configured fallback if enabled, and nothing else. Disabled or missing providers are left out, so a
    failed or disabled active provider with fallback off leaves AI tasks without a provider: candidates stay
    unreviewed and are never auto-executed in AI mode."""
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1)
    if c is None or not c.single_active:
        return None
    hops = []
    if c.active_provider_id:
        h = _hop(db.get(AIProvider, c.active_provider_id), c.active_model)
        if h:
            hops.append(h)
    if c.fallback_enabled and c.fallback_provider_id and c.fallback_provider_id != c.active_provider_id:
        h = _hop(db.get(AIProvider, c.fallback_provider_id), c.fallback_model)
        if h:
            hops.append(h)
    return hops


def chain_for(task: str) -> list[Hop]:
    s = get_settings()
    with session_scope() as db:
        single = single_active_chain(db)
        if single is not None:
            return single
        route = db.get(AITaskRoute, task)
        if route and route.chain:
            hops = []
            for item in route.chain:
                p = db.get(AIProvider, item.get("provider_id"))
                if p is None or not p.enabled:
                    continue
                try:
                    key = _decrypt(p.api_key_enc)
                except Exception as exc:  # noqa: BLE001 - an unreadable key skips the provider, visibly
                    log.warning("provider %s: stored key unreadable (%s)", p.name, type(exc).__name__)
                    continue
                hops.append(Hop(p.name, p.kind, item.get("model") or "", p.base_url, key, float(p.timeout_s),
                                int(p.max_retries), p.price_in_per_mtok, p.price_out_per_mtok, p.max_requests_per_minute))
            return hops
    if s.anthropic_api_key:
        return [Hop(ENV_PROVIDER, "anthropic", s.claude_model, None, s.anthropic_api_key, 180.0, 2, None, None)]
    return []


def route_mode(task: str) -> str:
    """'fallback' (original: first valid answer in chain order) or 'consensus'."""
    try:
        with session_scope() as db:
            if single_active_chain(db) is not None:
                return "fallback"          # Single Active AI: one provider; consensus needs two
            r = db.get(AITaskRoute, task)
            mode = (r.mode if r and r.chain else None) or "fallback"
    except Exception:  # noqa: BLE001
        return "fallback"
    return mode if mode == "consensus" and task in CONSENSUS_TASKS else "fallback"


def run_consensus(task: str, hops: list[Hop], system: str, user: dict, schema: dict, model_cls: type[BaseModel],
                  max_tokens: int = 16000, provider_factory=None, sleep=time.sleep) -> list[tuple]:
    """Ask the first two *different* providers of the chain independently (each with its own retries, no fallback to
    a third). Returns [(output | None, meta | None, error | None), ...] for both; the caller combines them."""
    from concurrent.futures import ThreadPoolExecutor
    pair, seen = [], set()
    for h in hops:
        if h.name not in seen:
            pair.append(h)
            seen.add(h.name)
        if len(pair) == 2:
            break
    if len(pair) < 2:
        raise GatewayError("consensus mode needs two different enabled providers in the route")

    def one(h: Hop):
        try:
            out, meta = run_hops(task, [h], system, user, schema, model_cls, max_tokens, provider_factory, sleep)
            return out, meta, None
        except GatewayError as exc:
            return None, None, f"{h.name}: {exc}"
    with ThreadPoolExecutor(2) as ex:
        return list(ex.map(one, pair))


def combine_reviews(engine_direction: str | None, reviews: list) -> tuple[str, str]:
    """Consensus rule for two boundary-checked reviews: confirm only when both confirm the engine's direction; any
    rejection rejects; anything else waits. Returns (decision, reason)."""
    ds = [r.decision for r in reviews]
    if any(d == "NO_TRADE" for d in ds):
        return "NO_TRADE", "at least one provider rejected the setup"
    if all(d in ("BUY", "SELL") for d in ds) and len(set(ds)) == 1 and ds[0] == engine_direction:
        return ds[0], "both providers confirmed the engine's direction"
    return "WAIT_FOR_CONFIRMATION", f"providers did not agree ({' / '.join(ds)})"


def configured(task: str) -> bool:
    return bool(chain_for(task))


def _provider(h: Hop, factory):
    if factory is not None:
        return factory(h)
    if h.kind == "anthropic":
        return AnthropicProvider(h.api_key, h.timeout_s)
    return OpenAICompatibleProvider(h.base_url or "", h.api_key, h.timeout_s)


def _take_slot(h: Hop) -> bool:
    """Client-side rate limit (Phase 14): False when this provider already made ``rpm`` requests this minute. If Redis
    is unreachable the call is allowed (the provider's own 429 handling still applies)."""
    if not h.rpm:
        return True
    try:
        from app.services.events import get_redis
        r = get_redis()
        key = f"rr:ai:rpm:{h.name}:{int(time.time() // 60)}"
        n = r.incr(key)
        r.expire(key, 90)
        return int(n) <= h.rpm
    except Exception:  # noqa: BLE001
        return True


def _cooling(name: str) -> bool:
    try:
        from app.services.events import get_redis
        return bool(get_redis().exists(f"rr:ai:cooldown:{name}"))
    except Exception:  # noqa: BLE001
        return False


def _note_failure(name: str, kind: str, retry_after: float | None) -> None:
    try:
        from app.services.events import get_redis
        r = get_redis()
        if kind == "rate_limited":
            r.set(f"rr:ai:cooldown:{name}", "rate_limited", ex=int(max(retry_after or 0, COOLDOWN_S)))
            return
        n = r.incr(f"rr:ai:fails:{name}")
        r.expire(f"rr:ai:fails:{name}", 300)
        if n >= FAILS_BEFORE_COOLDOWN:
            r.set(f"rr:ai:cooldown:{name}", "failing", ex=COOLDOWN_S)
    except Exception:  # noqa: BLE001
        pass


def _note_success(name: str) -> None:
    try:
        from app.services.events import get_redis
        get_redis().delete(f"rr:ai:fails:{name}")
    except Exception:  # noqa: BLE001
        pass


def _record(task: str, h: Hop, model: str, status: str, tin: int, tout: int, latency: float, attempts: int,
            error: str | None, fallback_from: str | None, cache_read: int = 0, cache_write: int = 0) -> None:
    cost = None
    if h.price_in is not None and h.price_out is not None and status == "ok":
        cost = round(tin / 1e6 * h.price_in + tout / 1e6 * h.price_out, 6)
    try:
        with session_scope() as db:
            db.add(AIUsage(task=task, provider=h.name, model=model or h.model or "?", requested_model=h.model or None,
                           status=status, input_tokens=tin,
                           output_tokens=tout, cache_read_tokens=int(cache_read or 0),
                           cache_write_tokens=int(cache_write or 0), cost=cost, latency_ms=round(latency, 1), attempts=attempts,
                           error=(error or None) and error[:1000], fallback_from=fallback_from))
    except Exception:  # noqa: BLE001 - accounting must not break the call
        log.exception("ai usage record failed")


def run(task: str, system: str, user: dict, schema: dict, model_cls: type[BaseModel], max_tokens: int = 16000,
        provider_factory=None, sleep=time.sleep, cache_ttl: int = 0) -> tuple[BaseModel, dict]:
    """Run one AI task through its route. Returns (validated output, meta)."""
    if task not in TASKS:
        raise GatewayError(f"unknown AI task {task!r}")
    hops = chain_for(task)
    if not hops:
        raise GatewayError(f"no AI provider is configured for {task}")
    return run_hops(task, hops, system, user, schema, model_cls, max_tokens, provider_factory, sleep, cache_ttl)


# ---- autonomous agent upgrade: API budget, de-duplication and result cache ------------------------------------------
# Seconds a validated answer is reused for an identical request (same task, prompt, input and providers), for the call
# sites that opt in. Everything else (provider tests, research, diagnostics, proposals) always calls the provider.
CACHE_TTL = {"candidate_review": 900, "engine_assistant": 120}


def budget_status() -> dict:
    """Spend today and this month (UTC) from the usage ledger against the configured limits. ``exhausted`` names the
    limit reached; None while AI calls are allowed. Unpriced providers count tokens only."""
    from datetime import datetime as _dt, timezone as _tz

    from sqlalchemy import func, select

    from app.db.models_ai import AIControl
    now = _dt.now(_tz.utc)
    day, month = now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(day=1, hour=0, minute=0, second=0,
                                                                                    microsecond=0)
    with session_scope() as db:
        c = db.get(AIControl, 1)
        spend_day = float(db.scalar(select(func.coalesce(func.sum(AIUsage.cost), 0.0)).where(AIUsage.created_at >= day)) or 0)
        spend_month = float(db.scalar(select(func.coalesce(func.sum(AIUsage.cost), 0.0))
                                      .where(AIUsage.created_at >= month)) or 0)
        tokens_day = int(db.scalar(select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0))
                                   .where(AIUsage.created_at >= day)) or 0)
        pct = int(c.budget_alert_pct) if c and c.budget_alert_pct else 80
        lim = {"daily_budget_usd": c.daily_budget_usd if c else None,
               "monthly_budget_usd": c.monthly_budget_usd if c else None,
               "daily_token_limit": c.daily_token_limit if c else None}
    exhausted = None
    if lim["daily_budget_usd"] is not None and spend_day >= lim["daily_budget_usd"]:
        exhausted = f"daily budget {lim['daily_budget_usd']} USD reached ({spend_day:.4f})"
    elif lim["monthly_budget_usd"] is not None and spend_month >= lim["monthly_budget_usd"]:
        exhausted = f"monthly budget {lim['monthly_budget_usd']} USD reached ({spend_month:.4f})"
    elif lim["daily_token_limit"] is not None and tokens_day >= lim["daily_token_limit"]:
        exhausted = f"daily token limit {lim['daily_token_limit']} reached ({tokens_day})"
    alert = None
    for used, limit, name in ((spend_day, lim["daily_budget_usd"], "daily budget"),
                              (spend_month, lim["monthly_budget_usd"], "monthly budget"),
                              (tokens_day, lim["daily_token_limit"], "daily token limit")):
        if limit and used >= limit * pct / 100:
            alert = f"{name}: {round(used / limit * 100)}% used ({used:.4f} of {limit})"
            break
    return {"spend_today_usd": round(spend_day, 6), "spend_month_usd": round(spend_month, 6), "tokens_today": tokens_day,
            **lim, "alert_pct": pct, "alert": alert, "exhausted": exhausted}


def budget_alert() -> str | None:
    """A warning incident (de-duplicated by the incident system) when spend passes the alert threshold."""
    try:
        b = budget_status()
        if b.get("alert"):
            from app.services import incidents
            with session_scope() as db:
                incidents.record(db, "ai", "warning", f"AI budget alert: {b['alert']}",
                                 {"exhausted": b.get("exhausted")}, category="ai_budget")
        return b.get("alert")
    except Exception:  # noqa: BLE001 - alerts never break an AI call
        log.debug("budget alert failed", exc_info=True)
        return None


def _cache_key(task: str, hops: list[Hop], system: str, payload, max_tokens: int) -> str:
    import hashlib
    body = json.dumps({"t": task, "h": [(h.name, h.model) for h in hops], "s": system, "p": payload, "m": max_tokens},
                      sort_keys=True, default=str)
    return "rr:ai:cache:" + hashlib.sha256(body.encode()).hexdigest()


def run_hops(task: str, hops: list[Hop], system: str, user: dict, schema: dict, model_cls: type[BaseModel],
             max_tokens: int = 16000, provider_factory=None, sleep=time.sleep, cache_ttl: int = 0) -> tuple[BaseModel, dict]:
    payload = redact(user)
    validator = strict(model_cls)
    errors, previous = [], None
    try:
        bud = budget_status()
    except Exception:  # noqa: BLE001 - accounting problems never block the engine
        bud = {"exhausted": None}
    if bud.get("exhausted") and hops:
        _record(task, hops[0], hops[0].model, "skipped", 0, 0, 0, 0, f"AI budget: {bud['exhausted']}", None)
        raise GatewayError(f"AI budget reached: {bud['exhausted']} (the engine continues without AI)")
    # Opt-in per call site (``cache_ttl``): provider tests, research and diagnostics must always reach the provider.
    ttl = int(cache_ttl or 0)
    key = _cache_key(task, hops, system, payload, max_tokens) if ttl > 0 else None
    if key:
        try:
            from app.services.events import get_redis
            hit = get_redis().get(key)
            if hit:
                data = json.loads(hit)
                out = validator.model_validate(data["out"])
                meta = {**data["meta"], "cached": True, "input_tokens": 0, "output_tokens": 0, "attempts": 0}
                h0 = next((h for h in hops if h.name == meta.get("provider")), hops[0])
                _record(task, h0, meta.get("model") or h0.model, "cached", 0, 0, 0, 0, None, None)
                return out, meta
        except Exception:  # noqa: BLE001 - a cache problem only means a fresh call
            log.debug("AI cache read failed", exc_info=True)
    for h in hops:
        if _cooling(h.name):
            _record(task, h, h.model, "skipped", 0, 0, 0, 0, "cooling down after failures or a rate limit", previous)
            errors.append(f"{h.name}: cooling down")
            continue
        attempts, last = 0, None
        while attempts <= h.max_retries:
            if not _take_slot(h):
                last = ProviderError(f"local limit of {h.rpm} requests per minute reached", "local_limit")
                break
            attempts += 1
            t0 = time.perf_counter()
            try:
                comp = _provider(h, provider_factory).complete_json(system, payload, schema, h.model, max_tokens)
            except ProviderError as exc:
                last = exc
                if exc.kind == "rate_limited" and (exc.retry_after or 0) > MAX_RETRY_AFTER:
                    break
                if exc.retryable and attempts <= h.max_retries:
                    sleep(min(exc.retry_after or 0.5 * 2 ** (attempts - 1), MAX_RETRY_AFTER))
                    continue
                break
            try:
                out = validator.model_validate(comp.data)
            except ValidationError as exc:
                _record(task, h, comp.model, "invalid_output", comp.input_tokens, comp.output_tokens, comp.latency_ms,
                        attempts, f"schema validation failed: {exc.error_count()} errors", previous)
                _note_failure(h.name, "invalid_output", None)
                errors.append(f"{h.name}: invalid output")
                last = None
                break
            _record(task, h, comp.model, "ok", comp.input_tokens, comp.output_tokens, comp.latency_ms, attempts, None,
                    previous, comp.cache_read_tokens, comp.cache_write_tokens)
            _note_success(h.name)
            budget_alert()
            meta = {"provider": h.name, "model": comp.model, "input_tokens": comp.input_tokens,
                    "output_tokens": comp.output_tokens, "cache_read_tokens": comp.cache_read_tokens, "latency_ms": comp.latency_ms, "attempts": attempts,
                    "fallback_from": previous}
            if key:
                try:
                    from app.services.events import get_redis
                    get_redis().set(key, json.dumps({"out": out.model_dump(), "meta": meta}, default=str), ex=ttl)
                except Exception:  # noqa: BLE001
                    log.debug("AI cache write failed", exc_info=True)
            return out, meta
        if last is not None:
            status = "rate_limited" if last.kind in ("rate_limited", "local_limit") else "error"
            _record(task, h, h.model, status, 0, 0, (time.perf_counter() - t0) * 1000 if attempts else 0, attempts,
                    str(last), previous)
            if last.kind != "local_limit":          # our own limit is not a provider failure
                _note_failure(h.name, last.kind, last.retry_after)
            errors.append(f"{h.name}: {last}")
        previous = h.name
    raise GatewayError("; ".join(errors) or "no provider produced a valid answer")


# ---- tasks ---------------------------------------------------------------------------------------------------------
class GatewayReviewer:
    """Candidate review through the gateway. Same interface the scanner uses for ClaudeAgent (throttle_ok, review,
    model), and the same boundaries for every provider."""
    model = "ai-gateway"

    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory

    def throttle_ok(self, symbol: str, timeframe: str) -> bool:
        from app.agents.claude_agent import ClaudeAgent
        return ClaudeAgent.throttle_ok(self, symbol, timeframe)

    def review(self, candidate: dict):
        from app.agents.claude_agent import REVIEW_SCHEMA, SYSTEM_PROMPT, AgentError, Review, enforce_boundaries
        from app.services.events import agent_activity
        agent_activity("claude_agent", "analysing", f"Reviewing {candidate.get('symbol')} {candidate.get('timeframe')} "
                                                    f"{candidate.get('direction')}")
        if route_mode("candidate_review") == "consensus":
            return self._review_consensus(candidate)
        try:
            review, meta = run("candidate_review", SYSTEM_PROMPT, {"task": "review_candidate", "candidate": candidate},
                               REVIEW_SCHEMA, Review, provider_factory=self.provider_factory,
                               cache_ttl=CACHE_TTL["candidate_review"])
        except GatewayError as exc:
            raise AgentError(str(exc)) from exc
        review = enforce_boundaries(candidate, Review.model_validate(review.model_dump()))
        agent_activity("claude_agent", "approved" if review.decision in ("BUY", "SELL") else "rejected",
                       f"{candidate.get('symbol')}: {review.decision} via {meta['provider']}")
        return review, {**meta, "model": f"{meta['provider']}/{meta['model']}"}


    def _review_consensus(self, candidate: dict):
        """Two providers review independently; each answer is schema-validated and boundary-checked on its own, then
        combined by ``combine_reviews``. If either provider gives no valid answer the candidate stays UNREVIEWED (an
        error), so it is never auto-executed on one opinion."""
        from app.agents.claude_agent import REVIEW_SCHEMA, SYSTEM_PROMPT, AgentError, Review, enforce_boundaries
        from app.services.events import agent_activity
        try:
            answers = run_consensus("candidate_review", chain_for("candidate_review"), SYSTEM_PROMPT,
                                    {"task": "review_candidate", "candidate": candidate}, REVIEW_SCHEMA, Review,
                                    provider_factory=self.provider_factory)
        except GatewayError as exc:
            raise AgentError(f"consensus: {exc}") from exc
        failed = [e for _, _, e in answers if e]
        if failed:
            raise AgentError("consensus: no valid answer from " + "; ".join(failed))
        reviews = [enforce_boundaries(candidate, Review.model_validate(o.model_dump())) for o, _, _ in answers]
        metas = [m for _, m, _ in answers]
        decision, why = combine_reviews(candidate.get("direction"), reviews)
        base = next((r for r in reviews if r.decision == decision), reviews[0])
        out = Review.model_validate({**base.model_dump(), "decision": decision,
                                     "explanation": f"[AI consensus: {why}] " + base.explanation})
        votes = [{"provider": m["provider"], "model": m["model"], "decision": r.decision} for r, m in zip(reviews, metas)]
        agent_activity("claude_agent", "approved" if decision in ("BUY", "SELL") else "rejected",
                       f"{candidate.get('symbol')}: {decision} by consensus of "
                       + " + ".join(v["provider"] for v in votes))
        meta = {"provider": "consensus(" + "+".join(v["provider"] for v in votes) + ")",
                "model": " + ".join(f"{v['provider']}/{v['model']}" for v in votes),
                "input_tokens": sum(m["input_tokens"] for m in metas), "output_tokens": sum(m["output_tokens"] for m in metas),
                "latency_ms": max(m["latency_ms"] for m in metas), "attempts": sum(m["attempts"] for m in metas),
                "fallback_from": None, "mode": "consensus",
                "consensus": {"votes": votes, "decision": decision, "rule": why}}
        return out, meta


def make_reviewer(provider_factory=None) -> GatewayReviewer | None:
    """A reviewer if candidate review has any provider (a route, or the original RR_ANTHROPIC_API_KEY), else None:
    candidates then stay UNREVIEWED and are never auto-executed (original conservative rule)."""
    return GatewayReviewer(provider_factory) if configured("candidate_review") else None
