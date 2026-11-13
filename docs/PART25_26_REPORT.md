# Part 25 + 26 — Implementation report

Branch: `feature/part25-26-ai-intelligence`. Baseline: `docs/PART25_26_BASELINE.md`. Upgrade, verification and
rollback commands: `docs/PART25_26_UPGRADE.md`.

This report states only what was implemented and measured. No accuracy or profitability is claimed: every model
and consensus weight trained in this session was trained on simulated data and was **correctly rejected**.

## Phase summary

| Phase | Scope | Key files | Status |
|---|---|---|---|
| 0 | Inspection, compatibility report, strategy inventory (215 IDs, config and code hashes) | `docs/PART25_26_BASELINE.md`, `tests/baseline/strategy_inventory.json`, `tests/test_preservation.py` | Done |
| 1 | Additive schema: 19 new tables, nullable columns, one FK, reversible stage mapping | `app/db/models_intel.py`, `migrations/versions/0002_part25_26_intelligence.py` | Done; up → down → up verified on a populated DB |
| 2 | Trade collection: MT5 deal ledger, incremental sync (6 h overlap), partial-exit VWAP aggregation, DEMO/LIVE separation; features (MAE/MFE in R) | `app/learning_engine/collector.py`, `features.py` | Done |
| 3 | Performance analysis by 13 dimensions; Wilson and bootstrap 95% CIs; verdicts (INSUFFICIENT_DATA below 30 trades) | `app/learning_engine/performance.py` | Done |
| 4 | Learning memory (evidence-counted strengths and weaknesses) and failure analysis; Claude receives `historical_evidence` | `memory.py`, `failure.py`, `app/scanner/service.py`, `app/agents/claude_agent.py` | Done |
| 5 | Regime intelligence: Gaussian HMM (causal filtering), CUSUM change points, 13-regime probabilities, benchmark against an ATR-percentile baseline | `app/intelligence/regime.py` | Done |
| 6 | Multi-specialist consensus: prior mode over independent evidence families; validated weights only from ≥ 200 outcome-linked decisions plus approval; advisory by default | `app/intelligence/consensus.py`, `app/learning_engine/consensus_weights.py` | Done |
| 7 | Probability and uncertainty: triple-barrier dataset, embargoed chronological splits, 5 candidate model families, isotonic calibration, Brier/BSS/ECE/AUC, significance bounds, "LOW DATA CONFIDENCE" | `app/learning_engine/datasets.py`, `models.py`, `app/intelligence/probability.py` | Done |
| 8 | Model governance: TRAINED → VALIDATED → APPROVED → PRODUCTION, RETIRED, rollback; SHA-256-checked artifacts; shadow mode; drift detection (PSI, calibration, performance) | `models.py`, `drift.py`, `/api/learning/models*` | Done |
| 9 | Claude strategy discovery (structured output) with duplicate detection | `app/agents/strategy_discovery/agent.py` | Implemented; **not called live** (no API key in this environment) |
| 10 | Sandboxed experimental strategies: AST allow-list, subprocess with rlimits, scrubbed environment, restricted builtins | `app/strategies/experimental/{validator,sandbox,runner}.py` | Done |
| 11 | Validation: 60/20/20 split, walk-forward (4 folds), Monte Carlo, spread ×1/2/3, slippage 0/2/5, benchmark, Bonferroni-adjusted bound; final test runs only after validation passes; demo forward-test stage | `app/strategies/experimental/runner.py`, `lifecycle.py` | Done |
| 12 | Strategy lifecycle DISCOVERED … PRODUCTION / REJECTED / DISABLED with approvals and rollback | `lifecycle.py`, `/api/learning/experiments*` | Done |
| 13 | Liquidity and anomaly intelligence (9 kinds); critical anomalies block **entries only** | `app/intelligence/anomaly.py`, `app/risk/engine.py` (`market_anomaly`) | Done |
| 14 | Portfolio risk intelligence: rolling correlation passed to the existing correlation check; effective independent bets; can only tighten | `app/intelligence/portfolio.py` | Done |
| 15 | Entry/SL/TP and failure intelligence: stop-widening refused, adjustments logged, missed opportunities kept separate | `app/execution/manager.py`, `failure.py` | Done |
| 16 | Strategy DNA / duplicate report (report only) | `app/intelligence/fingerprint.py` | Done |
| 17 | Execution analytics: slippage, fill latency, requotes and rejections by retcode | `app/intelligence/execution_analytics.py` | Done |
| 18 | Scanner extension: regime, consensus, probability and anomalies per analysis | `app/scanner/service.py` | Done |
| 19 | Scheduler: separate `learner` service, Redis lock, idempotent `job:bucket` keys, retries, timeouts, manual triggers; no ticks sent to Claude | `app/learning_engine/scheduler.py`, `docker-compose.yml` | Done |
| 20 | APIs: 35 additive routes under `/api/learning/*` and `/api/intelligence/*`; all 71 original routes unchanged | `app/api/routes/learning.py`, `docs/API.md` | Done |
| 21 | Dashboards: **AI Learning & Strategy Evolution** (`/learning`) and **Institutional Predictive Intelligence Center** (`/predictive`, 8 tabs plus anomalies) | `frontend/app/(console)/{learning,predictive}`, `components/Intel.tsx` | Done; build passes (24 pages) |
| 22 | Tests: unit, integration, safety and performance | `tests/test_{learning,intelligence,discovery_sandbox,preservation,performance}.py` | See results |

## Safety test matrix

| Requirement | Enforced by | Test |
|---|---|---|
| The 215 original strategies are unchanged (IDs, config, code hash) | Inventory snapshot | `test_preservation.py` |
| No experimental code in the production registry | Registry scan for `exp_` | `test_preservation.py` |
| Sandbox rejects imports outside the allow-list, eval/exec/open, dunder access and globals | AST validator | `test_discovery_sandbox.py` |
| Sandbox resource and time limits; no network or secrets in its environment | rlimits, scrubbed environment | `test_discovery_sandbox.py` |
| A critical anomaly blocks new entries; existing positions are untouched | `ControlState.anomaly_block` | `test_intelligence.py` |
| A stale quote blocks entry | `_stale_limit()` | `test_intelligence.py` |
| A stop-loss cannot be widened; tightening is logged | `manager._log_adjustment` | `test_intelligence.py` |
| Portfolio intelligence cannot raise limits | Only passes `corr` to the existing check | `test_intelligence.py` |
| Enforce-mode consensus can only downgrade to WAIT | `consensus.apply` | `test_intelligence.py` |
| Noise models are rejected | Bootstrap lower bounds on skill | `test_intelligence.py` |
| No fabricated data | Missing inputs give `None` or `INSUFFICIENT_DATA` / `LOW DATA CONFIDENCE` | `test_learning.py`, `test_intelligence.py` |
| DEMO/LIVE learning separation | `mode` column and query filters | `test_learning.py` |
| A deal ledger that does not reconcile (exit volume > entry, exit before entry, wrong-side exit, mixed symbols) never produces an outcome; the row is flagged `INCONSISTENT`, keeps its recorded values and is excluded from learning | `collector.aggregate_position`, `performance.learnable()` | `test_learning.py` |
| Grid and martingale stay disabled | Original risk checks unchanged | Original `test_risk.py` |

## Measured performance (local container, PostgreSQL 16, single process)

| Operation | Time |
|---|---|
| Regime intelligence, 500 bars | 76 ms |
| Probability inference | 11.7 ms |
| Consensus | 0.5–1 ms |
| Scanner analysis, one symbol × timeframe, all enabled strategies plus intelligence | 290 ms |
| Learning and intelligence API calls | 9–17 ms |

## Live verification (fake MT5 connector, full stack)

* The learner ran every job.
  * `train_probability` produced v1 from 2,451 rows → **REJECTED**, as expected on random data.
  * Consensus weights → **REJECTED** (low data).
  * `discovery` → **SKIPPED** (no Claude key).
  * The 4 reference experiments passed their unit tests and reached **EXPERIMENTAL**.
* Trades were opened, partially closed and closed through the existing Risk Engine and execution path.
  * Each trade aggregated into one `closed_positions` row with `partial_exits` recorded.
  * Each trade was classified and received a failure report.
* The anomaly gate correctly blocked every entry while the first test feed was inconsistent.
* The original consecutive-loss lock tripped after five spread-only losses.
  * For the test environment only, the DEMO limit was raised through the audited API.
  * The LIVE setting was not changed.
* **Defect found and fixed during the live round.** The fake terminal was restarted, which reset its ticket
  counter, so new positions reused old position IDs. The collector merged deals from two different positions and
  recorded R multiples of 87 and 35. The collector now refuses to derive outcomes from a ledger that does not
  reconcile, as described in the safety matrix above. On the live dev database both rows were flagged
  `INCONSISTENT` and excluded from learning, and the dashboard dropped from 5 trades to 3. Real MT5 does not
  reuse position IDs, but the guard also covers broker-side ledger anomalies.
* The new pages render with no console errors.

## Test results (final run)

| Suite | Result |
|---|---|
| Backend (`pytest`, includes the original suite plus Part 25/26 tests) | **146 passed** |
| MT5 connector (`pytest`) | **10 passed** |
| Frontend (`next build`) | Passed, 24 pages |
| Migration 0002 upgrade → downgrade → upgrade | Passed |

## Outstanding issues (honest status)

1. **Claude was not called live**: there is no API key in this environment. Discovery and code generation are tested with stubs and report `SKIPPED` without a key. The "Claude research" button returns 409 in that case instead of being hidden.
2. **No model has been validated**: no real broker history was available. Predictive value is unproven until a model passes the out-of-sample gates on real data.
3. **Consensus runs in advisory mode** and uses prior (unvalidated) weights until ≥ 200 outcome-linked decisions exist.
4. **Untested on a real Windows MT5 terminal**: all execution tests used the FakeMT5 connector.
5. **Frontend advisory**: `npm audit` still reports a moderate postcss advisory (inherited from Next.js); nothing changed here.
6. **Healthcheck script**: `scripts/healthcheck.sh` does not yet check the `learner` service; verify it with `docker compose ps learner`.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Original strategies, IDs, logic, history, APIs, DEMO/LIVE separation, risk engine and kill switch preserved | Met (tests) |
| Additive migrations only, reversible | Met (cycle verified) |
| Learning from actual MT5 outcomes only, with confidence intervals | Met |
| Weights only from validated evidence | Met (prior mode until validated) |
| Calibration metrics and LOW DATA CONFIDENCE | Met |
| AI code never runs in the production process | Met (sandbox subprocess plus registry test) |
| Validation with OOS, walk-forward, Monte Carlo, costs and multiple-testing protection | Met |
| Lifecycle with approvals and rollback | Met |
| Demonstrated predictive accuracy or profitability | **Not claimed.** Requires out-of-sample evidence on real data. |
