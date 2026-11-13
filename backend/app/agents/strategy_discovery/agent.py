"""Claude Strategy Research & Development agent.

research(): reviews the registry + measured performance and proposes new methodologies (each checked
against existing strategies for duplication before it is stored as DISCOVERED).
develop(): asks Claude to implement one proposal against the sandbox API. The returned source is only
statically validated and stored — it is executed exclusively inside the sandbox subprocess.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.agents.claude_agent import AgentError, ClaudeAgent, SYSTEM_PROMPT
from app.db.models import AgentConversation, StrategyExperiment
from app.intelligence.fingerprint import proposal_overlap
from app.strategies import registry
from app.strategies.experimental import lifecycle

METHODOLOGIES = ["Hurst exponent", "Hidden Markov models", "Kalman filters", "Entropy-based trading", "Fractal market analysis",
                 "Z-score trading", "Gaussian process models", "Hawkes processes", "Bayesian strategy selection",
                 "Volatility regime switching", "Multi-factor momentum", "Adaptive statistical models", "Cointegration",
                 "Statistical arbitrage", "Change-point detection", "Wavelet analysis", "Causal analysis",
                 "Reinforcement learning (research/simulation only)"]

_STR = {"type": "string"}
_ARR = {"type": "array", "items": {"type": "string"}}
PROPOSAL_FIELDS = ["name", "category", "concept", "mathematical_foundation", "market_conditions", "entry_conditions",
                   "exit_conditions", "stop_loss_logic", "take_profit_logic", "timeframes", "required_data", "advantages",
                   "weaknesses", "risks", "implementation_complexity", "testing_method", "closest_existing_strategies"]
RESEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["gaps", "weaknesses", "duplicates", "proposals"],
    "properties": {
        "gaps": _ARR, "weaknesses": _ARR, "duplicates": _ARR,
        "proposals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": PROPOSAL_FIELDS,
            "properties": {k: (_ARR if k in ("timeframes", "required_data", "advantages", "weaknesses", "risks",
                                             "closest_existing_strategies") else _STR) for k in PROPOSAL_FIELDS}}},
    },
}
DEVELOP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["source", "notes"],
                  "properties": {"source": _STR, "notes": _STR}}

SANDBOX_API = """Implement ONE Python class deriving from `Strategy` (already in scope, do not import it).
Names available without import: Strategy, StrategySignal, candle, ind (indicators: ema, sma, atr, rsi, macd, adx,
bollinger, keltner, donchian, zscore, linreg_slope, ...), st (structure: swings, structure_events, fair_value_gaps,
order_blocks, ...), np, pd. You may import only math, numpy, pandas, statistics.
Required class attributes: code = "exp_<snake_case>", name, category = "experimental", component (one of
market_structure, smc_ict, price_action, trend, indicator, liquidity, volume), params (dict), min_bars (<= 280).
Implement detect(self, ctx) returning self.make(ctx, "BUY"|"SELL", stop_or_None, target=..., reasons=[...]) or None.
ctx.df holds closed OHLCV bars up to the decision bar (ctx.close, ctx.ema(n), ctx.atr(), ctx.rsi(), ctx.adx(),
ctx.known_swings(), ctx.events(), ctx.fvgs()). Never use future data, files, network, eval/exec, dunder attributes,
getattr/setattr, globals, or global state. Keep it deterministic."""


class Proposal(BaseModel):
    name: str
    category: str
    concept: str
    mathematical_foundation: str
    market_conditions: str
    entry_conditions: str
    exit_conditions: str
    stop_loss_logic: str
    take_profit_logic: str
    timeframes: list[str]
    required_data: list[str]
    advantages: list[str]
    weaknesses: list[str]
    risks: list[str]
    implementation_complexity: str
    testing_method: str
    closest_existing_strategies: list[str]


def research(db: Session, performance_summary: dict, agent: ClaudeAgent | None = None, max_new: int = 3) -> dict:
    agent = agent or ClaudeAgent()
    reg = [{"code": m.code, "name": m.name, "category": m.category, "regimes": m.regimes, "description": m.description[:160]}
           for m in registry.metas()]
    system = SYSTEM_PROMPT + f"""

You are now the quantitative research lead. Using the registry and measured performance provided, identify gaps,
weaknesses and duplicated logic, then propose at most {max_new} genuinely new methodologies (consider: {', '.join(METHODOLOGIES)}).
Before proposing, check the registry: do not propose anything equivalent to an existing strategy under a new name;
list the closest existing strategies for each proposal. Proposals are research only and will be implemented and
validated in an isolated sandbox (backtest, out-of-sample, walk-forward, Monte Carlo, costs, DEMO forward test)
before any human approval. Use only data an MT5 retail feed provides (OHLC, tick volume, spread) unless you mark
the required data explicitly."""
    conv = AgentConversation(purpose="DISCOVERY", model=agent.model, request={"registry": len(reg)})
    db.add(conv)
    db.flush()
    try:
        data, meta = agent._call(system, {"task": "strategy_research", "registry": reg, "performance": performance_summary},
                                 RESEARCH_SCHEMA, max_tokens=32000)
        proposals = [Proposal.model_validate(p) for p in data["proposals"]][:max_new]
    except (AgentError, ValidationError, KeyError) as exc:
        conv.error = str(exc)[:2000]
        raise AgentError(str(exc)) from exc
    conv.response, conv.input_tokens, conv.output_tokens = data, meta["input_tokens"], meta["output_tokens"]
    created = []
    for p in proposals:
        text = " ".join([p.name, p.concept, p.entry_conditions, p.mathematical_foundation])
        overlap = proposal_overlap(text, db)
        dup = [o for o in overlap if o["overlap"] >= 0.45]
        exp = StrategyExperiment(title=p.name[:255], hypothesis=p.concept, proposal=p.model_dump(),
                                 stage="REJECTED" if dup else "DISCOVERED", evidence={}, duplicate_of=overlap)
        db.add(exp)
        db.flush()
        created.append({"id": exp.id, "name": p.name, "duplicate": bool(dup), "closest": overlap[:3]})
    return {"gaps": data["gaps"], "weaknesses": data["weaknesses"], "duplicates": data["duplicates"], "proposals": created}


def develop(db: Session, exp: StrategyExperiment, agent: ClaudeAgent | None = None, user_id: int | None = None) -> dict:
    agent = agent or ClaudeAgent()
    system = SYSTEM_PROMPT + "\n\nYou are now a senior quant developer. " + SANDBOX_API
    conv = AgentConversation(purpose="DEVELOPMENT", model=agent.model, request={"experiment_id": exp.id})
    db.add(conv)
    db.flush()
    try:
        data, meta = agent._call(system, {"task": "implement_strategy", "proposal": exp.proposal,
                                          "hypothesis": exp.hypothesis}, DEVELOP_SCHEMA, max_tokens=24000)
    except AgentError as exc:
        conv.error = str(exc)[:2000]
        raise
    conv.response = {"notes": data["notes"], "source_chars": len(data["source"])}
    conv.input_tokens, conv.output_tokens = meta["input_tokens"], meta["output_tokens"]
    if lifecycle.normalise(exp.stage) not in ("DISCOVERED", "DEVELOPING"):
        raise ValueError(f"cannot develop an experiment in stage {exp.stage}")
    check = lifecycle.attach_source(db, exp, data["source"], user_id, "generated by Claude")
    return {"static": check, "notes": data["notes"], "next": "run sandboxed unit tests" if check["ok"] else "fix static errors"}


_ = json
