# Part 25 + 26 — Phase 1 baseline & compatibility report

Starting point: `main` @ `0293bb3`, working tree clean (no uncommitted changes to preserve).
Feature branch: `feature/part25-26-ai-intelligence`.

## Baseline test results (before any change)
| Suite | Result |
|---|---|
| backend (`pytest`, PostgreSQL 16 + Redis) | 100 passed |
| mt5-connector | 10 passed |
| frontend (`next build`, strict TS) | OK, 22 static pages |
| Alembic | `0001 (head)` |

Original strategy inventory: 215 strategies. The code, category, parameters, timeframes, regimes,
requirements and source hash of each one are frozen in `backend/tests/baseline/strategy_inventory.json`
and enforced by `tests/test_preservation.py`.

## Existing architecture (reused, not replaced)
| Requested capability | Already present | Reuse decision |
|---|---|---|
| Trade history | `closed_positions` (from MT5 deals, linked to `execution_id`), `trade_orders`, `trade_executions` | **Extend.** Add an incremental raw-deal ledger (`mt5_deals`) and enrichment (`trade_features`). `closed_positions` stays the single source of truth for outcomes. |
| Strategy performance | `strategy_performance` (OOS / walk-forward rows from backtests) | **Extend.** Live/demo snapshots go into the new `performance_snapshots` table, which adds dimensions and confidence intervals. |
| Calibration | `signals.engine.Calibrator` (isotonic, min 30 samples) | **Reuse** inside the new probability engine. |
| Regime | `analysis.regime.classify` (rule-based: ADX/EMA/ATR/BB/variance ratio) | **Keep as the baseline**, wrapped by the new regime intelligence (probabilities, HMM, change points). |
| Claude discovery | `agents.claude_agent.discover()` → `strategy_experiments` (stages RESEARCH…ADMIN_APPROVAL) | **Extend.** New lifecycle stages (old names mapped), source code, sandbox validation, approvals. |
| Backtesting | `backtesting.engine` (next-bar fills, costs, OOS, walk-forward, Monte Carlo, optimiser) | **Reuse** unchanged inside the sandbox runner. |
| Risk | `risk.engine.evaluate` with an optional `corr` matrix (previously unused) | **Extend only by adding checks.** A new anomaly gate, and the `corr` matrix now supplied by portfolio intelligence. No existing check is loosened. |
| Execution metrics | `trade_executions` (retcode, slippage, latency) | **Reuse** for execution analytics (no new table). |
| Claude review | `claude_agent.review` with boundaries | **Extend.** The candidate now includes retrieved historical evidence and consensus. Boundaries are unchanged. |
| Scheduling | Single trading worker with a Redis lock | **Add** a separate `learner` process with its own lock, so research jobs can never block trading. |

## Part 25 / Part 26 implementation status before this work
* **Partially present:** Claude discovery (proposals only, no code generation or validation), strategy
  performance from backtests, isotonic calibrator, rule-based regime, execution metrics stored but not
  analysed.
* **Missing:**
  * Incremental deal sync and partial-exit aggregation.
  * Trade feature enrichment, MAE/MFE, and performance analysis by dimension with confidence intervals.
  * Learning memory and evidence retrieval for Claude.
  * Regime probabilities, stability and transitions.
  * Specialist consensus.
  * ML dataset, training, model registry and probability engine.
  * Anomaly detection, portfolio correlation intelligence, failure intelligence, fingerprinting.
  * Drift detection, sandboxed experimental strategies, lifecycle and approvals.
  * Learning dashboards and the background learner.

## Duplicate-functionality risks avoided
* No second trades table: enrichment references `closed_positions.id`.
* No second regime classifier replacing the original: the rule-based one remains the benchmark baseline.
* No second experiments table: `strategy_experiments` is extended with additive columns.
* No second execution log: analytics read `trade_executions`.

## Migration plan
`0002_part25_26` is **additive only**: new tables, plus nullable columns added to `closed_positions` and
`strategy_experiments`, plus a value mapping of experiment stage names. No drops, renames or type changes.
Its downgrade removes only what 0002 added.
