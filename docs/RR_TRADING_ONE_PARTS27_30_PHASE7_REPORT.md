# RR Trading One — Parts 27–30, Phase 7 Report (Part 30: reliability, data governance, capital protection, recovery)

**Date:** 2026-11-17 · **Branch:** `claude/focused-cori-b8em6u` · **Commits:** `318f80e`, `9c04a30` (defect fix), `de3074f` (MSRV fix)
**Plan:** `docs/PARTS27_30_AUDIT.md` (Part 30 rows)

## 1. Summary

| Req. | Class | Delivered |
|---|---|---|
| 30.1 Reconciliation | B | Startup compares the broker's pending orders with the execution journal, using the connector's tag (magic + comment): matched, **unknown engine orders** (block entries until resolved), foreign (reported, never touched), no longer at the broker |
| 30.1 Queue monitoring | B | Learner jobs overdue (> 2 × period) or stuck (> 2 × timeout) and the last scan cycle. The monitor raises a system event and notification at most hourly. |
| 30.3 Data checks | B | Per analysis: duplicate and out-of-order candles, invalid prices, weekday gaps (FX weekend excluded), quote age and last-bar age (broker clock against the engine's), a 0–100 score with stated penalties, and the source (`mt5:<symbol>:<tf>`). Recorded with each analysis and in the decision trace. |
| 30.3 / 30.7 Dataset versions | B | Every trained model (probability, consensus weights, adaptive profile) records `dataset_version`, a SHA-256 of its exact training data |
| 30.4 Risk Governor | B | `max_floating_loss_pct`, `max_strategy_risk_pct` and `volatility_sizing` (budget reduced to as little as ×0.25 at high ATR percentile, never increased). **All off by default.** The kill switch now **cancels the engine's own pending orders**: journalled, idempotent CANCEL_PENDING; foreign orders never touched. |
| 30.5 Scenario simulation | E | Simulation Laboratory: base, continuation, reversal, volatility ×2, spread ×3, slippage shock and gap through the stop, walked on the bars after the entry, results in R |
| 30.6 Counterfactuals | E | Alternative stops (×0.5–2), targets (1–3 R), exit timing (6–48 bars) and limit entries (0.25 / 0.5 R better, only if traded at), on the bars that followed. **Labelled hindsight.** |
| 30.7 Governance | B | Model validation expiry (`RR_MODEL_VALIDATION_MAX_AGE_DAYS`, 90): degraded, and in enforce mode BUY/SELL → WAIT. Strategy suspension **recommendations** (≥ 30 paper/live trades and PF < 0.8); disabling stays your decision. |
| 30.8 Monitoring | B | API request count, mean latency and 5xx rate (15 min), ERROR/CRITICAL log counts per service, database connections by state against the maximum, job backlog |
| 30.9 Scheduled backups and restore tests | B | The desktop app takes a daily automatic backup (newest 14 kept) and a weekly restore test of the newest backup into a scratch database: all live tables present, same schema version, live database untouched. Both run only while PostgreSQL is already running. Status shown under Backup and Recovery. |

**What changes trading behaviour by default:**
* **Startup reconciliation** now blocks entries if the broker holds engine-tagged pending orders the journal does not know.
* **The kill switch** also cancels the engine's own pending orders.

Both are protective. Everything else is off, advisory, or research only.

## 2. Defect found and fixed (class D)

**What the new backlog monitor found on the installed stack:**
* A `portfolio` learner job had been **RUNNING for 12 hours** with a 300 s timeout.
* Cause: a learner killed outright (here, the sandbox restart the day before) leaves its row RUNNING. RUNNING counts as final, so the row was never retried or cleaned up.

**Fix (`9c04a30`):**
* The learner is single-instance: its lock expires 300 s after its holder dies.
* So at start any RUNNING row belongs to a dead process, and is marked INTERRUPTED (retryable).

**Verified on the install:** row 128 became INTERRUPTED, the learner logged the recovery, and the backlog now shows nothing stuck. A regression test covers it.

## 3. Tests and results

| Suite | Result |
|---|---|
| Engine, full (alone, local, on `318f80e`) | **394 passed** (378 + 16), 0 failed |
| Learner-related files after the fix | 64 passed (`test_part30.py` now 17) |
| Desktop core (`cargo test`) | **133 passed** (130 + 3); `cargo clippy -D warnings` clean after the MSRV fix |
| MT5 connector / Windows connector | 17 passed / 36 passed (the shared simulator gained pending orders and their removal) |
| CI on `318f80e` | backend ✅ 44058843072, frontend ✅ 44058842903, windows-connector ✅ 44058842885, **desktop ❌ 44058843220** (see below) |

**Desktop CI failure.** Clippy on the Windows build refused `Option::is_none_or` (stable since Rust 1.82; the project's minimum is 1.77). It was replaced by an equivalent match in `de3074f`. Its CI result is in §7.

New tests cover:
* **Reconciliation and emergency:**
  * the engine's and connector's tag and magic agree;
  * matched / foreign / unknown pending orders through the real bridge, with unknown ones failing startup;
  * the kill switch cancelling only the engine's pending orders, idempotently.
* **Risk Governor:** new checks absent by default; floating-loss limit; strategy allocation; volatility sizing reducing volume.
* **Data and governance:** data-quality detection of every problem kind, the weekend not counted, the scan recording the score; dataset versions; validation expiry, drift, and suspension (with < 30 trades never suspended, and nothing disabled).
* **Monitoring:** request, error and log counters; backlog alerting once.
* **Simulation:** trade path outcomes, scenarios, hindsight-labelled counterfactuals, and the API from an opportunity and from a closed position (permissions, 422s).
* **Desktop:** daily/weekly scheduling, never starting the database, a failing restore test, pruning to 14.

**Test fixes during development:**
* A volatility-sizing test hit the existing "unknown news blocks entries" policy. The test sets news to clear rather than loosening the policy.
* The simulation API test used the global bridge. It now overrides the dependency, as other API tests do.

## 4. End-to-end on the installed application

1. **Upgrade:** the database was at 0013 (78 analyses, 25 signals, 215 strategies, 401 closed trades). After `supervise restart_all` it is at **0014**, every row kept, DEMO, auto trading off.
2. **Live reconciliation:** the monitor's startup checks now report "pending orders compared: 0 matched, 0 unknown engine order(s), 0 foreign, 0 no longer at the broker" and pass.
3. **Monitoring via the gateway:**
   * 13 requests, mean 57.6 ms, 0 server errors;
   * 5 of 100 database connections;
   * backlog: **one stuck job**, which led to the defect fix in §2. After the fix: none stuck, none overdue.
4. **Data quality:** after an event-driven re-analysis, EURUSD.r and XAUUSD.r H1 score **100**, quote age 0–0.1 s. Earlier analyses honestly read "not recorded (older analysis)".
5. **Governance:** the production probability model v5 is healthy (validated 3 days ago, no drift). Its `dataset_version` is empty because it was trained before this phase; the next training records it. No suspensions.
6. **Risk defaults:** the three capital-protection parameters read 0 / off.
7. **Not exercised on the install:**
   * the kill switch (it would change the install's state; tested end to end with the simulated broker);
   * simulations (the install's opportunities predate signal levels; tested end to end);
   * the desktop scheduler, which runs inside the Windows app (its logic is tested in Rust; the Windows CI build compiles the app).

## 5. Limitations

* Restore tests compare tables and the schema version, not row-by-row content. A backup is naturally older than the live database.
* Scenario "continuation", "reversal" and "volatility ×2" are mechanical transformations of the real path, not forecasts.
* Pending orders tagged by the engine but unknown to its journal are not cancelled automatically, on purpose: an operator decides.
* The scheduled maintenance runs in the desktop app. A remote (Ubuntu) deployment keeps its own backup scripts.

## 6. Security

* **New endpoints:**
  * `POST /api/simulations`: `backtest.run`, audited;
  * `GET /api/simulations`, `/api/governance`, `/api/data-quality`, `/api/system/monitoring`: session.
* **New desktop command:** `maintenance_status` (read-only), in the three ACL lists, verified by the existing test.
* **No limit loosened**; new limits default off. Cancelling pending orders under the kill switch only reduces risk.

## 7. CI after the fixes

To be confirmed on `de3074f` (desktop build including clippy and the Windows installer). Reported in the reply that follows this report.

## 8. Files

* **Created:**
  * engine: `backend/app/execution/reconcile.py`, `backend/app/intelligence/data_quality.py`, `backend/app/learning_engine/versioning.py`, `backend/app/learning_engine/governance.py`, `backend/app/services/monitoring.py`, `backend/app/backtesting/scenarios.py`;
  * migration and tests: `backend/migrations/versions/0014_simulation_runs.py`, `backend/tests/test_part30.py`;
  * console: `frontend/app/(console)/simulation/page.tsx`;
  * this report.
* **Modified:**
  * engine: `risk/engine.py`, `execution/manager.py`, `workers/main.py`, `workers/roles.py`, `scanner/service.py`, `intelligence/selection.py`, `learning_engine/{scheduler,models,consensus_weights,adaptive}.py`, `db/models_opportunity.py`, `api/routes/intel.py`, `core/config.py`, `main.py`;
  * tests: `tests/conftest.py`, `mt5-connector/tests/fake_mt5.py`;
  * desktop: `core/src/{supervisor,actions}.rs`, `src-tauri/src/lib.rs`, `build.rs`, `capabilities/main.json`, `ui/backup.js`, `ui/index.html`, `README.md`;
  * console: the System Monitor, Risk Centre and Shell;
  * manuals.

## 9. Rollback

Revert the commits and run `alembic downgrade 0013`. Automatic backups already taken stay in the backups folder (kind `auto`), as ordinary backups.

## 10. Next

**Phase 8: integration.** Cross-checking the whole Parts 27–30 flow end to end, updating the test matrix, and the dashboard additions still pending (the opportunity trace in the Command Centre and Signal Centre).
