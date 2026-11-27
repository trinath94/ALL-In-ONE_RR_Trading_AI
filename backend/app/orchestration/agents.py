"""The specialist agents of the RR Trading One control room (specification Part 9), as a closed registry.

They are **not** independent bots. Each entry maps an agent to the existing engine component that does the work
(reused, not duplicated) and states what it may do. A central orchestrator (`app.orchestration.orchestrator`) runs
them inside the engine's existing loops and records every run in the typed contract (`agent_runs`).

Rules that hold for all of them:
* deterministic quantitative code produces indicators, strategy outputs, scores and risk checks;
* LLMs are used only for contextual review/research of shortlisted, evidence-rich candidates, through the AI
  gateway, and their output is untrusted, schema-validated and boundary-checked;
* only `trade_execution` and `trade_management` act on the broker, only inside the trading worker / monitor, and only
  through the Risk Engine and the execution manager (idempotency, journal, PC guard);
* no agent can enable LIVE, change permissions, size a position outside the Risk Engine, or run commands.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

DETERMINISTIC, LLM, HYBRID = "deterministic", "llm", "hybrid"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    version: str
    kind: str                 # deterministic | llm | hybrid
    implementation: str       # the existing component that does the work
    output_schema: str
    runs_in: str              # which engine loop runs it
    authority: str            # what its output may influence
    execution_path: bool = False

    def dict(self) -> dict:
        return asdict(self)


A = AgentSpec
REGISTRY: list[AgentSpec] = [
    A("master_orchestrator", "Master Orchestrator", "1.0", DETERMINISTIC, "app.orchestration.orchestrator",
      "CycleSummary", "analysis service (each scan cycle)", "Coordinates and records the other agents; decides nothing about trades"),
    A("broker_discovery", "Broker Discovery Agent", "1.0", DETERMINISTIC, "app.services.trading.discover_symbols",
      "DiscoveryResult", "monitor service (after startup checks, every 6 h) and on request", "Symbol catalogue only"),
    A("symbol_scanner", "Symbol Scanner Agent", "1.0", DETERMINISTIC, "app.scanner.scheduler",
      "CycleReport", "analysis service", "Which symbol/timeframe jobs run, in what order"),
    A("original_strategy_analysis", "Original Strategy Analysis Agent", "1.0", DETERMINISTIC,
      "app.strategies.registry.evaluate_all (215 original strategies)", "StrategyOutcomes", "analysis service",
      "Strategy signals into the signal engine"),
    A("smc_ict", "SMC/ICT Analysis Agent", "1.0", DETERMINISTIC, "app.intelligence.consensus (SMC, ICT, LIQUIDITY specialists)",
      "SpecialistView", "analysis service (BUY/SELL/WAIT candidates)", "One consensus vote"),
    A("price_action", "Price Action Agent", "1.0", DETERMINISTIC, "app.intelligence.consensus (PRICE_ACTION specialist)",
      "SpecialistView", "analysis service (BUY/SELL/WAIT candidates)", "One consensus vote"),
    A("indicator_analysis", "Indicator Analysis Agent", "1.0", DETERMINISTIC,
      "app.intelligence.consensus (INDICATORS, QUANT, VOLUME_ORDER_FLOW specialists)", "SpecialistView",
      "analysis service (BUY/SELL/WAIT candidates)", "One consensus vote"),
    A("market_regime", "Market Regime Agent", "1.0", DETERMINISTIC, "app.analysis.regime + app.intelligence.regime",
      "RegimeView", "analysis service", "Regime label and strategy suitability"),
    A("multi_timeframe", "Multi-Timeframe Agent", "1.0", DETERMINISTIC, "StrategyContext.htf_bias (higher-timeframe bars)",
      "HtfBias", "analysis service", "Higher-timeframe bias in the signal context"),
    A("historical_performance", "Historical Performance Agent", "1.0", DETERMINISTIC,
      "app.scanner.service.strategy_stats + app.learning_engine.memory.retrieve_evidence", "HistoricalEvidence",
      "analysis service", "Out-of-sample statistics and evidence for reviews"),
    A("strategy_ranking", "Strategy Ranking Agent", "1.0", DETERMINISTIC, "app.scanner.service.scan_once (candidate ranking)",
      "RankedCandidates", "analysis service", "Which candidates are shortlisted for AI review"),
    A("strategy_correlation", "Strategy Correlation Agent", "1.0", DETERMINISTIC,
      "app.intelligence.consensus (evidence families) + /api/strategies-analytics/correlation", "ConsensusAgreement",
      "analysis service (BUY/SELL/WAIT candidates)", "Counts correlated evidence once"),
    A("signal_quality", "Signal Quality Agent", "1.0", HYBRID,
      "app.signals.engine.build_signal + app.intelligence.anomaly + app.intelligence.fusion; AI review of "
      "shortlisted candidates via app.ai.gateway",
      "SignalScore / Review", "analysis service",
      "Score and grade; the AI review may only confirm, downgrade to WAIT or reject (enforce_boundaries)"),
    A("news_events", "News and Economic Events Agent", "1.0", DETERMINISTIC, "app.news.service.status_for",
      "NewsStatus", "analysis service", "News restriction status"),
    A("portfolio_risk", "Portfolio Risk Intelligence Agent", "1.0", DETERMINISTIC,
      "app.risk.engine (pre-trade checks) + app.intelligence.portfolio", "RiskDecision", "trading worker",
      "Blocks orders that fail risk checks; cannot loosen limits"),
    A("ai_learning", "AI Learning Agent", "1.0", HYBRID,
      "app.learning_engine.scheduler (learner jobs); plain-language explanations via app.ai.gateway (learning_explanation)",
      "LearningJobResult", "learner service", "Model candidates; promotion needs validation + admin approval"),
    A("adaptive_optimisation", "Adaptive Optimisation Agent", "1.0", DETERMINISTIC,
      "app.learning_engine.consensus_weights + app.learning_engine.adaptive", "WeightProposal",
      "learner service; shadow/production weighting in each scan",
      "Proposals only; nothing applied without approval"),
    A("strategy_discovery", "Strategy Discovery Agent", "1.0", LLM,
      "app.agents.strategy_discovery + /api/agent/discover + research summaries, via app.ai.gateway", "Discovery",
      "on request (admin)",
      "Research proposals into the isolated experimental sandbox only"),
    A("trade_execution", "Trade Execution Agent", "1.0", DETERMINISTIC,
      "app.execution.manager.open_position (via the trading worker)", "ExecutionResult", "trading worker only",
      "Submits orders only for Claude-confirmed, Risk-Engine-approved signals; idempotent", execution_path=True),
    A("trade_management", "Trade Management Agent", "1.0", DETERMINISTIC,
      "app.workers.main.manage_positions (break-even, trailing, expiry)", "ManagementActions", "monitor service",
      "Protective modifications of open positions only", execution_path=True),
    A("post_trade_learning", "Post-Trade Learning Agent", "1.0", HYBRID,
      "app.learning_engine.failure + performance (learner jobs); AI trade research via app.ai.gateway (trade_research)", "PostTradeAnalysis", "learner service",
      "Failure analysis and statistics; read-only"),
    # Parts 27-30 Phase 4 (Part 27): views computed from data the engine already has, recorded in each opportunity's
    # decision trace. None of them can size, submit or loosen anything.
    A("fundamental_analysis", "Fundamental Analysis Agent", "1.0", DETERMINISTIC,
      "app.intelligence.opportunity.fundamental_view (economic calendar)", "FundamentalView", "analysis service",
      "Calendar context in the decision trace; 'unavailable' without a healthy calendar, never 'no news'"),
    A("entry_validation", "Entry Validation Agent", "1.0", DETERMINISTIC, "app.intelligence.opportunity.entry_validation",
      "EntryValidation", "analysis service",
      "Wrong-side stop/target -> NO_TRADE; price/spread checks advisory, or downgrade to WAIT with RR_OPPORTUNITY_GATE=enforce"),
    A("exit_optimisation", "Exit Optimisation Agent", "1.0", DETERMINISTIC, "app.intelligence.opportunity.exit_plan",
      "ExitPlan", "analysis service", "Exit plan from the setup's levels and the Risk Engine's own parameters; read-only"),
    A("failure_prediction", "Failure Prediction Agent", "1.0", DETERMINISTIC,
      "app.intelligence.opportunity.failure_prediction", "FailureRisk", "analysis service",
      "Named risk factors (not a probability); HIGH downgrades to WAIT only with RR_OPPORTUNITY_GATE=enforce"),
    A("ai_consensus", "AI Consensus Agent", "1.0", LLM, "app.ai.gateway (route mode 'consensus': two providers answer "
      "independently)", "ConsensusReview", "analysis service (shortlisted candidates)",
      "Confirms only when both providers confirm the engine's direction; otherwise the more cautious answer"),
    # Parts 27-30 Phase 5 (Part 28)
    A("pattern_recognition", "Chart Pattern Recognition Agent", "1.0", DETERMINISTIC,
      "app.patterns (harmonic and conservative Elliott detectors beside the 215 strategies; detection records)",
      "PatternDetections", "analysis service",
      "Detections, lifecycle, strategy relations and outcome statistics; advisory, never changes a decision"),
]
BY_ID = {a.id: a for a in REGISTRY}
# 21 original agents (Part 9) + 5 added in Parts 27-30 Phase 4 + 1 in Phase 5. The count is pinned so an agent cannot
# be added or duplicated by accident.
ORIGINAL_AGENTS = 21
assert len(REGISTRY) == ORIGINAL_AGENTS + 6 and len(BY_ID) == len(REGISTRY)

# Consensus specialists -> control-room agents
SPECIALIST_AGENT = {"SMC": "smc_ict", "ICT": "smc_ict", "LIQUIDITY": "smc_ict", "PRICE_ACTION": "price_action",
                    "INDICATORS": "indicator_analysis", "QUANT": "indicator_analysis",
                    "VOLUME_ORDER_FLOW": "indicator_analysis"}
# Learner jobs (app.learning_engine.scheduler.JOBS) -> agents
LEARNER_AGENT = {"trade_sync": "ai_learning", "drift": "ai_learning", "train_probability": "ai_learning",
                 "failure_analysis": "post_trade_learning", "performance_snapshot": "post_trade_learning",
                 "portfolio": "portfolio_risk", "fingerprints": "strategy_correlation",
                 "consensus_weights": "adaptive_optimisation", "adaptive_weights": "adaptive_optimisation", "experiments": "strategy_discovery",
                 "discovery": "strategy_discovery", "config_profiles": "adaptive_optimisation",
                 "improvement_research": "adaptive_optimisation", "playbooks": "adaptive_optimisation"}
