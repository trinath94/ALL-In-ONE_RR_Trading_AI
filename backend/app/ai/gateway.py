"""Unified AI provider gateway (RR Trading One Phase 9, specification Parts 9 and 14).

One path for every AI call, whatever the provider:

1. **Route.** The task's route (console: AI Providers) lists providers and models in order. With no route, the
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

import logging
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

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
}
# Tasks the engine calls today. The others can be routed, and are used by later phases.
WIRED_TASKS = ("candidate_review", "strategy_discovery", "diagnostics")
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


def _decrypt(enc: str | None) -> str | None:
    if not enc:
        return None
    from app.core.security import decrypt_ai_key
    return decrypt_ai_key(enc)


def chain_for(task: str) -> list[Hop]:
    s = get_settings()
    with session_scope() as db:
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
                                int(p.max_retries), p.price_in_per_mtok, p.price_out_per_mtok))
            return hops
    if s.anthropic_api_key:
        return [Hop(ENV_PROVIDER, "anthropic", s.claude_model, None, s.anthropic_api_key, 180.0, 2, None, None)]
    return []


def configured(task: str) -> bool:
    return bool(chain_for(task))


def _provider(h: Hop, factory):
    if factory is not None:
        return factory(h)
    if h.kind == "anthropic":
        return AnthropicProvider(h.api_key, h.timeout_s)
    return OpenAICompatibleProvider(h.base_url or "", h.api_key, h.timeout_s)


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
            error: str | None, fallback_from: str | None) -> None:
    cost = None
    if h.price_in is not None and h.price_out is not None and status == "ok":
        cost = round(tin / 1e6 * h.price_in + tout / 1e6 * h.price_out, 6)
    try:
        with session_scope() as db:
            db.add(AIUsage(task=task, provider=h.name, model=model or h.model or "?", status=status, input_tokens=tin,
                           output_tokens=tout, cost=cost, latency_ms=round(latency, 1), attempts=attempts,
                           error=(error or None) and error[:1000], fallback_from=fallback_from))
    except Exception:  # noqa: BLE001 - accounting must not break the call
        log.exception("ai usage record failed")


def run(task: str, system: str, user: dict, schema: dict, model_cls: type[BaseModel], max_tokens: int = 16000,
        provider_factory=None, sleep=time.sleep) -> tuple[BaseModel, dict]:
    """Run one AI task through its route. Returns (validated output, meta)."""
    if task not in TASKS:
        raise GatewayError(f"unknown AI task {task!r}")
    hops = chain_for(task)
    if not hops:
        raise GatewayError(f"no AI provider is configured for {task}")
    return run_hops(task, hops, system, user, schema, model_cls, max_tokens, provider_factory, sleep)


def run_hops(task: str, hops: list[Hop], system: str, user: dict, schema: dict, model_cls: type[BaseModel],
             max_tokens: int = 16000, provider_factory=None, sleep=time.sleep) -> tuple[BaseModel, dict]:
    payload = redact(user)
    validator = strict(model_cls)
    errors, previous = [], None
    for h in hops:
        if _cooling(h.name):
            _record(task, h, h.model, "skipped", 0, 0, 0, 0, "cooling down after failures or a rate limit", previous)
            errors.append(f"{h.name}: cooling down")
            continue
        attempts, last = 0, None
        while attempts <= h.max_retries:
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
                    previous)
            _note_success(h.name)
            return out, {"provider": h.name, "model": comp.model, "input_tokens": comp.input_tokens,
                         "output_tokens": comp.output_tokens, "latency_ms": comp.latency_ms, "attempts": attempts,
                         "fallback_from": previous}
        if last is not None:
            status = "rate_limited" if last.kind == "rate_limited" else "error"
            _record(task, h, h.model, status, 0, 0, (time.perf_counter() - t0) * 1000, attempts, str(last), previous)
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
        try:
            review, meta = run("candidate_review", SYSTEM_PROMPT, {"task": "review_candidate", "candidate": candidate},
                               REVIEW_SCHEMA, Review, provider_factory=self.provider_factory)
        except GatewayError as exc:
            raise AgentError(str(exc)) from exc
        review = enforce_boundaries(candidate, Review.model_validate(review.model_dump()))
        agent_activity("claude_agent", "approved" if review.decision in ("BUY", "SELL") else "rejected",
                       f"{candidate.get('symbol')}: {review.decision} via {meta['provider']}")
        return review, {**meta, "model": f"{meta['provider']}/{meta['model']}"}


def make_reviewer(provider_factory=None) -> GatewayReviewer | None:
    """A reviewer if candidate review has any provider (a route, or the original RR_ANTHROPIC_API_KEY), else None:
    candidates then stay UNREVIEWED and are never auto-executed (original conservative rule)."""
    return GatewayReviewer(provider_factory) if configured("candidate_review") else None
