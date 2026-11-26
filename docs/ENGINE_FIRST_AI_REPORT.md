# RR Trading One — Autonomous engine optimisation and Anthropic token reduction: report

**Date:** 2026-11-26 · **Branch:** `claude/focused-cori-b8em6u` · **Migration:** 0024 (additive)

The engine now makes every routine decision itself. The Anthropic API is an improvement service, called when the
engine finds something to improve or when you ask. Nothing was rebuilt or duplicated, and the 215 strategies, indicators, Risk Engine, MT5 connector and database are unchanged.

## 1. Audit: every Anthropic API call site (Phases 1–2)

| # | Where | Task | Before | How often (before) | After |
|---|---|---|---|---|---|
| 1 | `scanner/service.py` → `claude_review` | `candidate_review` | Every shortlisted BUY/SELL candidate of every scan was reviewed by the AI. In "With AI" mode auto-execution waited for it. | **Routine: every scan** (up to 5 candidates, per-symbol throttle 5 min) | **Removed by default.** The engine decides and the Risk Engine validates. Optional: *selective* (only candidates the engine is uncertain about, capped per day) or *all* (previous behaviour). |
| 2 | `services/assistant.py` | `engine_assistant` | Every chat answer was phrased by the AI when a provider was configured. | **Routine: every chat message** | **Removed by default.** The engine answers from its own records. AI phrasing is an opt-in setting. |
| 3 | `learning_engine/scheduler.py` → `job_discovery` | `strategy_discovery` | Weekly research. | Weekly | Unchanged (improvement). |
| 4 | `strategies/profiles.py` → `propose_ai` | `config_proposal` | On a user's request. | On demand | Also **engine-triggered** for weak strategies, max 2/day by default (§3). |
| 5 | `api/routes/learning.py` | `trade_research`, `learning_explanation`, `research_summary` | On a user's request. | On demand | Unchanged. |
| 6 | `api/routes/system.py` | `diagnostics`, `strategy_discovery` | On a user's request. | On demand | Unchanged. |
| 7 | `services/orchestrator.py` | 5 analysis tasks | On a chat analysis request. | On demand | Unchanged. |
| 8 | `api/routes/ai.py` → provider test | `diagnostics` | On the Test button. | On demand | Unchanged. |
| 9 | Claude Code (`ai/claude_code.py`) | Agent SDK | On a `/code` request. | On demand | Unchanged. |

**Already internal, with no AI call:**
* market scanning and broker symbol discovery;
* market data validation;
* multi-timeframe analysis;
* every indicator, including the SMC/ICT and pattern detectors;
* the 215 strategies, strategy selection and signal generation;
* the Risk Engine and execution through MT5;
* position management;
* trade collection, performance analytics and the learner's jobs;
* incident detection and self-healing;
* reports.

The audit found no AI call in any of these. They need no migration, only the two routine dependencies above.

**Result.** Routine operation (scans, signals, execution and chat) makes **zero** Anthropic calls by default. AI calls
come only from improvement research (default at most 2 a day), the weekly discovery, and what you ask for.

## 2. What was implemented

| Phase | Change |
|---|---|
| 1–3 | Audit above. Engine-first candidate handling: `services/trading.py` (`wants_ai_review`, `engine_decided_review`) and `scanner/service.py`. Engine-decided signals are executed in AI mode and in engine-only mode. A candidate the AI rejected is still never executed. |
| 4–5 | Indicator and strategy engines: preserved and unchanged. The existing preservation tests (IDs, source and configuration hashes) and the indicator/strategy suites pass. |
| 6 | Engine-owned learning: unchanged and in use. Trade sync, performance snapshots, failure reviews, loss significance (binomial test against validated evidence), drift, adaptive weights. |
| 7 | **Engine-triggered improvement workflow.** The learner's new `improvement_research` job, run daily: (1) the engine finds strategies whose recent losses exceed their evidence; (2) it sends one summarised research package (defaults, validated evidence by regime, the observed weakness, the objective), never raw data; (3) the AI proposes parameters and an optional regime filter; (4) the proposal is saved as a versioned profile, with source, provider/model, reason and the research package as evidence (`strategy_config_profiles.evidence`); (5) the existing `config_profiles` job validates it (out-of-sample sample size, expectancy, profit factor, drawdown, stability, walk-forward, doubled-cost test, look-ahead and leakage); (6) only a validated version can be activated, with rollback to its parent. It is capped per day, never repeats a strategy within 7 days, and stops at the AI budget. |
| 8 | Strategy discovery: the existing experiments pipeline (internal) and weekly AI discovery. The chat command queues the internal run. |
| 9–10 | Claude Code (Agent SDK) and the Engine Assistant: already in place (previous upgrade). The orchestrator now also routes improvement requests and the new runtime commands (§4). |
| 11 | API control: gateway with routes, budgets, alert, cache, de-duplication, retries/backoff, rate limits, ledger with cache tokens. New: candidate review policy, review cap, improvement cap, assistant AI switch. |
| 12–13 | Persistent runtime and recovery: role locks, startup reconciliation, idempotency keys, incident detection and self-healing, interrupted-task recovery (existing). Unchanged. |
| 14 | Independence: verified by tests. With no provider, every candidate is engine-decided and executed through the Risk Engine, and no AI call is made. |
| 15 | This report. |

## 3. Engine Assistant commands

| You type | Handled by |
|---|---|
| "Audit the complete engine and repair all errors." | Engine (operator agent: inspection, approved safe repairs) |
| "Reduce Anthropic token usage and move routine analysis to the internal engine." | Engine: autonomy report, what still depends on the AI, and the setting to change |
| "Check whether the engine can operate without Anthropic API." | Engine: the same autonomy report |
| "Analyze my trading history and identify weak strategies." | Engine: loss-significance test across all strategies |
| "Ask Anthropic to improve the existing strategy and save the proposed update." | Anthropic: proposal for the named or weakest strategy, saved and then validated by the engine |
| "Discover new strategy candidates and validate them." | Engine: queues the experiments run and reports the pipeline |
| "Improve the existing indicator library." | Claude Code (code change, reviewed patch) |
| "Complete the autonomous upgrade and start the internal runtime." | Engine: handover report with autonomy figures |

## 4. Dashboard (AI Studio → Engine autonomy)

The panel shows:
* the internal workload: scan cycles, analyses, signals, orders, learner jobs;
* candidates decided by the engine versus reviewed by the AI, and the engine's share;
* AI calls, tokens and cost, split into routine and improvement tasks, per task;
* the budget, and Claude Code task count and cost;
* improvement proposals with their status and validation;
* the engine-first settings.

## 5. Database

Migration 0024 is additive:
* `ai_control`: `candidate_review` (default and set to `off`), `review_daily_cap` (10), `review_confidence_below` (0.6), `assistant_ai` (false), `auto_improvement` (true), `improvement_daily_cap` (2).
* `strategy_config_profiles.evidence`.

No data is deleted. Rollback: `alembic downgrade 0023`. Expected version: `0024`.

## 6. Safety

* A signal the AI rejected is never executed. Engine-decided signals always pass the Risk Engine (kill switch, risk lock, limits, mandatory stop loss).
* Changing the candidate review while trading LIVE needs `trade.live`. LIVE itself stays behind its existing authorisation.
* AI proposals are never activated without the engine's validation, never overwrite a strategy's source or defaults, and keep the previous version for rollback.
* The AI budget stops AI calls; the engine continues.

## 7. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1–9 | Scanning, analysis, indicators, all strategies, signals, risk, MT5 execution, position management, history | Internal (existing). No AI call in routine operation, and that is tested. |
| 10–11 | Learns from performance; identifies weaknesses | Internal learner, loss-significance test |
| 12–14 | Anthropic proposes strategy/indicator configuration improvements; saved and versioned | Done (engine-triggered and on request) |
| 15 | Validated before activation | Existing validation gates and activation rules |
| 16 | Candidates tested before promotion | Existing experiment lifecycle |
| 17–18 | Claude Code repairs and upgrades; the Assistant controls both | Done (previous upgrade, extended routing) |
| 19 | API calls significantly reduced | Routine calls are zero by default: tested and shown on the dashboard |
| 20 | Usage and cost measurable | Ledger, dashboard |
| 21 | Operates when Anthropic is unavailable | Tested |
| 22–23 | Safe restart; no duplicate orders | Existing reconciliation and idempotency (tested suites) |
| 24 | Existing strategies and modules intact | Preservation suite |
| 25 | LIVE subject to explicit authorisation | Unchanged |

**Not verified here:**
* weeks of operation on your broker DEMO account;
* real token savings on your account (the dashboard will show them after you install);
* improvement proposals with your real trading history.

"Indicator improvement" is handled two ways:
* through strategy parameter proposals, which cover each strategy's indicator settings;
* through Claude Code, for changes to the indicator code itself.
