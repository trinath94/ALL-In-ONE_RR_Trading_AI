"""Engine Assistant orchestrator (Claude Code + Anthropic API integration).

Every chat request is routed to one of three handlers. The routing is deterministic (no AI is needed to decide), so it
works when Claude is unavailable:

* ``claude_code``: development work: audit or repair of *code*, changes, tests, migrations development, architecture.
  Runs through the Claude Agent SDK in the isolated workspace (``app.ai.claude_code``), with live progress,
  cancellation, resume, the engine's own test run, patch review and approval gates.
* ``anthropic_api``: analysis: market summaries, trade reviews, performance, strategy research, error explanations.
  One Messages API call through the AI gateway (route, budget, retries, cache, ledger), grounded only in engine facts.
* ``runtime``: everything about the running engine: status, signals, "why was it not executed", and the safe
  operator workflows (audit, repair, readiness, accounts, symbols, DEMO test, LIVE preparation, handover). Scanning,
  signals, risk, execution and position management stay with the engine itself; neither Claude path can drive them.

Prefixes force a route: ``/code``, ``/ai``, ``/engine``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

UTC = timezone.utc

API_KINDS = [  # (gateway task, pattern, title) - first match wins
    ("error_explanation", r"explain .*(error|incident|exception|failure|warning)|what does (this|the) (error|incident)|"
                          r"why (did|does) .*(fail|crash)", "Error explanation"),
    ("trade_review", r"review (my |the |recent |last |closed )*trades?|trade review|closed trades|"
                     r"how did (my|the) trades?", "Trade review"),
    ("performance_analysis", r"performance|win ?rate|profit factor|drawdown|expectancy|how (is|are) .*(doing|performing)",
     "Performance analysis"),
    ("strategy_research", r"strateg(y|ies) research|research .*strateg|which strateg|compare strateg|strategy idea",
     "Strategy research"),
    ("market_summary", r"market (summary|overview|analysis|update|conditions)|summari[sz]e .*market|"
                       r"what is the market|regime", "Market summary"),
]

ANALYSIS_SYSTEM = (
    "You are the analysis assistant of RR Trading One, a trading engine. Answer the operator using ONLY the facts in "
    "the JSON you are given. Quote numbers only as they appear there; if something is not in the facts, say it is not "
    "recorded. Never invent prices, backtest results, statistics or reasons, never promise or imply future "
    "profitability, and describe small samples as inconclusive. You cannot act on the engine: never claim to have "
    "changed anything, and never advise disabling safety controls, raising risk limits, removing stop losses, enabling "
    "LIVE trading or placing manual orders. End with concrete next steps the operator can take in the console.")

ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer", "next_steps"],
                 "properties": {"answer": {"type": "string", "maxLength": 4000},
                                "next_steps": {"type": "array", "items": {"type": "string", "maxLength": 300},
                                               "maxItems": 8}}}


class Answer(BaseModel):
    answer: str
    next_steps: list[str]


def route(question: str) -> tuple[str, str | None]:
    """(target, detail): target claude_code | anthropic_api | runtime; detail = the gateway task for anthropic_api."""
    q = (question or "").strip()
    low = q.lower()
    if low.startswith("/code"):
        return "claude_code", None
    if low.startswith("/engine"):
        return "runtime", None
    forced = low.startswith("/ai")
    for task, pat, _ in API_KINDS:
        if re.search(pat, q, re.I):
            return "anthropic_api", task
    if forced:
        return "anthropic_api", "market_summary"
    from app.ai import claude_code
    if claude_code.is_code_request(q):
        return "claude_code", None
    return "runtime", None


def title(task: str) -> str:
    return next((t for k, _, t in API_KINDS if k == task), task)


def _rows(objs, fields):
    return [{f: getattr(o, f, None) for f in fields} for o in objs]


def facts_for(db: Session, task: str) -> dict:
    """The engine facts an analysis may use (no credentials: everything also passes the gateway's redaction)."""
    from app.db.models import ClosedPosition, TradingSignal
    from app.db.models_opportunity import Incident
    from app.services import assistant
    since = datetime.now(UTC) - timedelta(days=30)
    facts: dict = {"generated_at": datetime.now(UTC).isoformat(), "overview": assistant.overview(db)}
    if task in ("market_summary", "strategy_research"):
        sigs = db.scalars(select(TradingSignal).order_by(TradingSignal.id.desc()).limit(30)).all()
        facts["recent_signals"] = _rows(sigs, ("id", "decision", "grade", "timeframe", "score",
                                               "calibrated_confidence", "regime", "status", "created_at"))
    if task in ("trade_review", "performance_analysis", "strategy_research"):
        closed = db.scalars(select(ClosedPosition).where(ClosedPosition.closed_at >= since)
                            .order_by(ClosedPosition.closed_at.desc()).limit(50)).all()
        facts["closed_trades_30d"] = _rows(closed, ("id", "account_id", "symbol", "direction", "volume", "open_price",
                                                    "close_price", "stop_loss", "profit", "commission", "swap",
                                                    "r_multiple", "opened_at", "closed_at", "close_reason", "mode"))
        agg = db.execute(select(ClosedPosition.mode, ClosedPosition.symbol, func.count(), func.sum(ClosedPosition.profit),
                                func.avg(ClosedPosition.r_multiple),
                                func.count().filter(ClosedPosition.profit > 0))
                         .where(ClosedPosition.closed_at >= since)
                         .group_by(ClosedPosition.mode, ClosedPosition.symbol)).all()
        facts["by_mode_symbol_30d"] = [{"mode": m, "symbol": s, "trades": n, "net_profit": p, "avg_r": r, "wins": w}
                                       for m, s, n, p, r, w in agg]
    if task == "error_explanation":
        inc = db.scalars(select(Incident).where(Incident.status != "RESOLVED").order_by(Incident.id.desc()).limit(25)).all()
        facts["open_incidents"] = _rows(inc, ("id", "category", "severity", "component", "title", "message",
                                              "explanation", "diagnosis", "recommended_fix", "occurrences", "status"))
    return json.loads(json.dumps(facts, default=str))


def analyse(db: Session, task: str, question: str, provider_factory=None, use_ai: bool = True) -> dict:
    """One Anthropic API analysis. Without a provider, an exhausted budget or an error, the engine's facts are still
    returned (the engine never depends on the answer)."""
    from app.ai import gateway
    q = re.sub(r"^/ai\s*", "", question.strip(), flags=re.I)
    facts = facts_for(db, task)
    out = {"intent": "analysis", "route": "anthropic_api", "analysis": task, "facts": facts}
    if not use_ai or not gateway.configured(task):
        return {**out, "source": "engine", "answer": f"{title(task)}: no AI provider is configured for this task "
                "(AI Studio). The engine's records are attached; the engine works without AI.", "next_steps": []}
    try:
        ai, meta = gateway.run(task, ANALYSIS_SYSTEM, {"question": q[:1000], "facts": facts}, ANSWER_SCHEMA, Answer,
                               max_tokens=4000, provider_factory=provider_factory)
    except gateway.GatewayError as exc:
        return {**out, "source": "engine", "ai_error": str(exc)[:300], "next_steps": [],
                "answer": f"{title(task)}: the AI is not available ({str(exc)[:160]}). The engine's records are "
                          "attached; trading is not affected."}
    return {**out, "source": "ai", "answer": ai.answer, "next_steps": ai.next_steps,
            "ai": {k: meta.get(k) for k in ("provider", "model", "input_tokens", "output_tokens", "cache_read_tokens",
                                            "latency_ms", "cached")}}
