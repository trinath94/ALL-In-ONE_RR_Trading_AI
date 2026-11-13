"""Claude AI Agent — Layer 3 reasoning over shortlisted, already-quantified opportunities.

Hard boundaries (enforced in code, not just in the prompt):
  * Claude sees only candidates the quantitative layers produced; it is never called per tick.
  * Claude can CONFIRM, DOWNGRADE (to WAIT) or REJECT a candidate. It cannot flip direction,
    upgrade NO_TRADE to a trade, change position size, or call the execution layer.
  * Its output is advisory input to the Risk Engine, which validates every order independently.
  * Confidence shown to users is the statistically calibrated value, never a model-generated number.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.core.config import get_settings
from app.services.events import agent_activity, get_redis

log = logging.getLogger("agent")

SYSTEM_PROMPT = """You are the market-intelligence reviewer inside RR Trading Engine, an institutional, risk-first \
automated trading system connected to MetaTrader 5.

Your job: review ONE trading candidate that deterministic quantitative layers have already detected and scored, and \
decide whether the evidence supports it. You receive the candidate, every strategy that fired (with its reasons), \
market structure, regime, multi-timeframe bias, session, spread, volatility, news status, historical out-of-sample \
strategy statistics, the risk configuration and open exposure.

Rules:
- Work only from the data provided. Never invent prices, levels, statistics or news. If something you would need is \
missing, say so and treat it as a reason for caution.
- Separate confirmed observations (facts present in the data) from hypotheses/predictions (your interpretation).
- You may CONFIRM the candidate's direction, DOWNGRADE it to WAIT_FOR_CONFIRMATION, or REJECT it as NO_TRADE. You \
cannot reverse its direction or turn a non-trade into a trade. NO_TRADE is a normal, often correct outcome.
- Penalise: conflicting strategies, counter-trend entries against higher-timeframe bias, strategies used outside \
their suited regime, poor or statistically thin out-of-sample history, wide spreads, unknown or restricted news \
risk, stops inside obvious liquidity, and correlated exposure already open.
- Do not output a probability or confidence percentage. The platform reports calibrated statistics separately.
- You do not size positions or place orders; an independent Risk Engine validates everything after you.
- Be concise and specific: cite the strategy codes and levels you relied on.
- The candidate may include `historical_evidence` retrieved from this platform's own trade database,
  `consensus` from independent specialists, a statistical `probability` estimate and `regime_intelligence`.
  Weigh evidence by its sample size: anything below 30 trades is inconclusive. A probability marked
  shadow, unavailable or LOW DATA CONFIDENCE must not be treated as support. Flag repeated loss causes
  that apply to this setup. Specialist disagreement is a reason for caution, not something to explain away."""

DECISIONS = ["BUY", "SELL", "NO_TRADE", "WAIT_FOR_CONFIRMATION"]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": DECISIONS},
        "market_structure": {"type": "string"},
        "higher_timeframe_bias": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "trade_management": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["decision", "market_structure", "higher_timeframe_bias", "observations", "hypotheses", "conflicts",
                 "invalidation_conditions", "trade_management", "explanation"],
    "additionalProperties": False,
}


class Review(BaseModel):
    decision: Literal["BUY", "SELL", "NO_TRADE", "WAIT_FOR_CONFIRMATION"]
    market_structure: str
    higher_timeframe_bias: str
    observations: list[str]
    hypotheses: list[str]
    conflicts: list[str]
    invalidation_conditions: list[str]
    trade_management: str
    explanation: str


class Param(BaseModel):
    name: str
    value: str


class Experiment(BaseModel):
    title: str
    hypothesis: str
    rationale: str
    components: list[str] = Field(default_factory=list)
    entry_rules: list[str]
    exit_rules: list[str]
    suited_regimes: list[str]
    parameters: list[Param] = Field(default_factory=list)
    test_plan: str


EXPERIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "performance_findings": {"type": "array", "items": {"type": "string"}},
        "disable_candidates": {"type": "array", "items": {"type": "string"}},
        "parameter_experiments": {"type": "array", "items": {"type": "string"}},
        "experiments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"}, "hypothesis": {"type": "string"}, "rationale": {"type": "string"},
                "components": {"type": "array", "items": {"type": "string"}},
                "entry_rules": {"type": "array", "items": {"type": "string"}},
                "exit_rules": {"type": "array", "items": {"type": "string"}},
                "suited_regimes": {"type": "array", "items": {"type": "string"}},
                "parameters": {"type": "array", "items": {
                    "type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["name", "value"], "additionalProperties": False}},
                "test_plan": {"type": "string"},
            },
            "required": ["title", "hypothesis", "rationale", "components", "entry_rules", "exit_rules",
                         "suited_regimes", "parameters", "test_plan"],
            "additionalProperties": False}},
    },
    "required": ["performance_findings", "disable_candidates", "parameter_experiments", "experiments"],
    "additionalProperties": False,
}


class Discovery(BaseModel):
    performance_findings: list[str]
    disable_candidates: list[str]
    parameter_experiments: list[str]
    experiments: list[Experiment]


class AgentError(Exception):
    pass


class ClaudeAgent:
    def __init__(self, client: anthropic.Anthropic | None = None):
        s = get_settings()
        self.model = s.claude_model
        self.effort = s.claude_effort
        self.client = client or anthropic.Anthropic(api_key=s.anthropic_api_key or None, max_retries=2, timeout=180)

    def _call(self, system: str, user: dict, schema: dict, max_tokens: int = 16000) -> tuple[dict, dict]:
        t0 = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                cache_control={"type": "ephemeral"},   # system prompt is stable -> cached prefix
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",                   # route around safety-classifier refusals
                messages=[{"role": "user", "content": json.dumps(user, default=str, separators=(",", ":"))}],
            )
        except anthropic.RateLimitError as exc:
            raise AgentError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AgentError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError(f"connection error: {exc}") from exc
        meta = {"model": resp.model, "latency_ms": (time.perf_counter() - t0) * 1000,
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                "stop_reason": resp.stop_reason}
        if resp.stop_reason == "refusal":
            raise AgentError("model declined the request (refusal)")
        if resp.stop_reason == "max_tokens":
            raise AgentError("response truncated (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AgentError("no text block in response")
        return json.loads(text), meta

    # -- Signal review ----------------------------------------------------------------------
    def throttle_ok(self, symbol: str, timeframe: str) -> bool:
        try:
            return bool(get_redis().set(f"rr:claude:{symbol}:{timeframe}", "1", nx=True,
                                        ex=get_settings().claude_min_interval_seconds))
        except Exception:  # noqa: BLE001 - if redis is down, be conservative and skip the call
            return False

    def review(self, candidate: dict) -> tuple[Review, dict]:
        agent_activity("claude_agent", "analysing", f"Reviewing {candidate.get('symbol')} {candidate.get('timeframe')} "
                                                    f"{candidate.get('direction')}")
        data, meta = self._call(SYSTEM_PROMPT, {"task": "review_candidate", "candidate": candidate}, REVIEW_SCHEMA)
        try:
            review = Review.model_validate(data)
        except ValidationError as exc:
            raise AgentError(f"invalid structured output: {exc}") from exc
        review = enforce_boundaries(candidate, review)
        agent_activity("claude_agent", "approved" if review.decision in ("BUY", "SELL") else "rejected",
                       f"{candidate.get('symbol')}: {review.decision}")
        return review, meta

    # -- Strategy discovery / performance review ---------------------------------------------
    def discover(self, performance: dict, registry_summary: list[dict]) -> tuple[Discovery, dict]:
        agent_activity("strategy_optimisation", "analysing", "Claude performance review & strategy discovery")
        sys = SYSTEM_PROMPT + """

For this task you act as a quantitative research lead. Using ONLY the performance statistics and strategy registry \
provided: identify the best and worst performers per regime/timeframe/session (citing sample sizes; treat < 30 trades \
as inconclusive), strategies that conflict or duplicate each other's logic, candidates for temporary disabling, and \
parameter experiments worth testing. Then propose at most 3 new experimental strategies or combinations, each with \
precise, programmable entry/exit rules and a test plan. Proposals are research only: they are isolated from \
production and must pass backtest, out-of-sample, walk-forward and paper trading plus administrator approval."""
        data, meta = self._call(sys, {"task": "performance_review_and_discovery", "performance": performance,
                                      "registry": registry_summary}, EXPERIMENT_SCHEMA, max_tokens=32000)
        try:
            return Discovery.model_validate(data), meta
        except ValidationError as exc:
            raise AgentError(f"invalid structured output: {exc}") from exc


def enforce_boundaries(candidate: dict, review: Review) -> Review:
    """Claude may only confirm, downgrade or reject — never flip or upgrade."""
    engine_dir = candidate.get("direction")
    engine_decision = candidate.get("decision")
    if engine_decision not in ("BUY", "SELL"):
        if review.decision in ("BUY", "SELL"):
            review.explanation = "[Boundary] Engine did not approve a trade; agent upgrade ignored. " + review.explanation
            review.decision = "WAIT_FOR_CONFIRMATION" if engine_decision == "WAIT" else "NO_TRADE"
        return review
    if review.decision in ("BUY", "SELL") and review.decision != engine_dir:
        review.explanation = "[Boundary] Agent suggested reversing direction; treated as NO_TRADE. " + review.explanation
        review.decision = "NO_TRADE"
    return review
