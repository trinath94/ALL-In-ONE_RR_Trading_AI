# RR Trading One — Phase 20 Report

**Phase:** 20 — Performance and resource management (specification Part 20)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 20 asks for an application usable on an ordinary Windows workstation. It lists:
* resource controls: parallel scanning, worker and learner limits, AI throttling;
* job handling: queues, priorities, cancellation, timeouts, retries;
* caching and database indexes;
* CPU, memory and disk monitoring, and scanner backpressure.

It also states three rules:
* learning must not starve the trading worker;
* AI research must not delay execution or position protection;
* no unrestricted 215 strategies × every symbol × every timeframe workload.

Phase 20 audited every item against the running engine, **measured** before changing anything, and fixed what the
measurements showed.

| Part 20 item | Before Phase 20 | Now |
|---|---|---|
| Configurable parallel scanning | ✅ Phase 8: `scan_max_parallel` (4), bounded thread pool | Unchanged |
| Scanner backpressure, priorities, timeouts | ✅ Phase 8: per-cycle budget (50 s); excess jobs deferred and run first next cycle; per-job timeout (45 s); per-job lock against double starts; priority order | Unchanged |
| No unrestricted 215 × symbols × timeframes | ✅ Phase 8: only symbols enabled for scanning; strategies only on timeframes they support; a pair is analysed only when the broker has closed a new bar | Unchanged |
| Worker and learner resource limits | ✅ Phase 3: each role has a CPU cap and a memory limit | **+ CPU priority** (section 2) |
| Learning must not starve the trading worker | ⚠️ Caps only: equal priority when the PC is busy | **Fixed and measured** (section 2) |
| AI request throttling | ✅ Phase 14: per-provider requests-per-minute limit, fallback without cooldown; ledger | Unchanged |
| AI research must not delay execution or position protection | ✅ Research runs in the learner container. The trading worker and the monitor never call an AI provider (the review is done earlier, by analysis). | **+ the learner now has the lowest CPU priority** |
| Background job queues, cancellation, timeouts, retries | ⚠️ Learner timeout only *marked* a job: its thread kept running, and the next loop started another copy. No cancellation. | **Fixed:** isolated job processes, real timeout, operator cancel, clean stop (section 3) |
| Retry policies | ✅ Learner: up to 3 attempts per period. Bridge reads: 1–2 retries with back-off; orders are never retried blindly (idempotency, Phase 15). AI: fallback chain. | Unchanged; documented |
| Data caching | ✅ Signed models cached after a signature check on every use; strategy registry; correlation matrix; the scanner skips bars it already analysed | Unchanged |
| Database indexes | ⚠️ The trading worker's 5-second poll, the monitor's signal expiry, the control room and the scanner page **read every row ever stored** | **Fixed:** migration 0011 and two query rewrites (section 4) |
| Memory, CPU and disk monitoring | ⚠️ A host-wide snapshot only | **New:** each role reports its own container's CPU and memory against its own limits; data disk; database size; alerts (section 5) |
| PostgreSQL resources | ❌ Docker's default 64 MB shared memory: **parallel VACUUM failed** on a grown database | **Fixed:** 256 MB (section 4) |

| Result | Value |
|---|---|
| End-to-end on the running install | **11 / 11** (section 6) |
| New backend tests | 12 (`test_resources.py`) |
| Backend suite, including the 215-strategy preservation test | **314 passed** (302 + 12 new) in one full run |
| Desktop core tests (Rust) | 106 passed (unchanged) |
| Frontend `next build` | Passed |
| CI (`7978cf7`) | frontend 43962564891 and desktop 43962564914 passed. Backend 43962565144 **failed one new test** (`test_a_timed_out_job_is_terminated_not_left_running`): on the slower CI runner the job's process took longer than the test's 2-second timeout just to start, so it was terminated (correctly) before writing the PID file the test waits for. The engine behaved as designed; the test now waits for the job to start and uses a 20-second timeout (fixed in Phase 21). |

## 2. CPU priority: learning cannot starve position protection

**Measurement.** A fixed CPU task (a 1-million-step loop) ran 20 times inside the **monitor** container, which
protects open positions. It was timed idle, then while the learner and analysis containers each ran four busy processes.

| PC (simulated by pinning every role to N CPUs) | Equal priority (before) | With priority (now) |
|---|---|---|
| 4 cores | p50 100 → 98 ms | — (no effect to fix) |
| 2 cores | p50 102 → **123 ms** | p50 100 → 99 ms |
| **1 core** | p50 100 → **193 ms** (doubled) | p50 99 → **100 ms** |

**Fix.** `infrastructure/compose.yml` keeps every CPU cap and adds `cpu_shares`. Caps limit what each role may use;
shares decide who wins when the PC is busy.

| Role | Priority |
|---|---|
| Monitor (position protection) | 4096 |
| Trading worker (execution) | 4096 |
| PostgreSQL | 2048 |
| Engine API | 1024 (default) |
| Analysis | 512 |
| Learner (training, research) | 256 |

The 1-core measurement was repeated on the updated install as part of the end-to-end run: p50 100 ms idle, 99 ms saturated.

## 3. Learner jobs: real timeout, cancellation and stop

**Defect.** The learner ran each job in a thread and waited for its timeout. A thread cannot be stopped in Python, so
a job that overran:
* was recorded TIMEOUT but **kept running in the background**;
* was **started again** on the next loop, because TIMEOUT allows a retry, while the first copy was still running.

A slow weekly training job (1-hour timeout) could therefore pile up copies of itself, competing for CPU and the
database.

**Fix (`app/learning_engine/scheduler.py`).** Every production learner job is **isolated**: each run is a separate
process (`spawn`, so it inherits no database connection or lock). The learner checks it every second and ends it, with
SIGTERM and then SIGKILL after 5 s:

| Event | Recorded as | Retried? |
|---|---|---|
| Timeout | TIMEOUT, "process terminated" | Yes, up to 3 attempts |
| An operator's cancel (`POST /api/learning/jobs/{name}/cancel`, `strategy.manage`, audited; a **cancel** button next to a running job in the Learning Centre) | CANCELLED | No, final for that period |
| The learner stopping (SIGTERM from Stop All or an upgrade) | INTERRUPTED | Yes, on the next start |

Previously a long job outlived the container's 30 s stop grace period and was killed without any record.

* Cancelling a job that is not running is refused (409), so a stray cancel can never hit a later run.
* Ad-hoc `Job`s keep the original in-process behaviour (`isolated=False`). The original engine's scheduler test runs unchanged and still passes.

## 4. Database: indexes, query shapes and shared memory

**Measurement.** A scratch copy of the install's database was grown with the install's own recorded rows to about
40 days of a 50-symbol, 3-timeframe scan:
* 248k analyses;
* 525k agent runs;
* 57k signals;
* 57k predictions.

The hot queries were timed with `EXPLAIN ANALYZE`:

| Query (how often) | Before | After | Change |
|---|---|---|---|
| Trading worker: new BUY/SELL signals (**every 5 s**) | 16.0 ms, full table scan | **0.07 ms** | partial index on `expires_at` where `status = 'NEW'` |
| Monitor: expire signals | 13.5 ms, full table scan | **0.01 ms** | same index |
| Control room: last run of each agent | 46.1 ms, scan of every run | **0.24 ms** | index `(agent_id, id)`; one lookup per registered agent instead of a GROUP BY over the whole log |
| Scanner page: latest analysis per symbol × timeframe | 43.6 ms, scan of every analysis | **2.8 ms** | index `(symbol_id, timeframe, id)`; one lookup per symbol × timeframe |
| Chart overlays: latest analysis of one pair | 2.1 ms | **0.03 ms** | same index |

Before, these costs grew with the history (roughly tenfold after a year); after, they do not depend on it. The two
rewritten queries return exactly what the old ones did (tests: one row per analysed pair, the latest; the latest run
of each registered agent).

**Shared memory.** Building that database, `VACUUM` failed with "could not resize shared memory segment … No space
left on device". PostgreSQL's parallel maintenance outgrew Docker's default 64 MB `/dev/shm`. On a user's PC the same
would have happened as the database grew. The database container now has `shm_size: 256m` (confirmed on the install).

## 5. CPU, memory and disk monitoring

`app/services/resources.py`:
* **What is reported.** Every 15 s, each role (monitor, analysis, trading worker, learner) reports **its own container's** usage. It is read from the Linux control group (cgroup v2 on Docker Desktop / WSL2; v1 supported too), with file cache excluded as `docker stats` does:
  * memory against the container's memory limit;
  * CPU against the container's CPU cap.
* **Missing reports.** Reports expire after 2 minutes: a role that stopped reporting shows **"no report"**, never healthy.
* **Thresholds:**
  * memory ≥ 85 % of its limit: WARN; ≥ 95 %: **CRITICAL** (about to be killed for running out of memory);
  * CPU at its cap: WARN (being throttled);
  * data disk < 5 GB free: WARN; < 2 GB: **CRITICAL** (PostgreSQL stops accepting writes on a full disk).
* **Where it shows:**
  * `GET /api/system/resources` returns the roles, the disk, the database size and the alerts;
  * CRITICAL conditions become **active critical alerts** on the Command Centre and mark it degraded;
  * the **System Monitor** page has a "Role resources" table.

## 6. Verification

**End-to-end on the running install (11 / 11):**
1. Monitor, analysis and learner reported real usage against their real limits (768 MB / 0.5 CPU, 2 GB / 2, 3 GB / 2). The stopped trading worker showed "no report". Disk and database size were reported, with no alert while healthy.
2. Schema 0011 with the three indexes. The worker's poll uses the partial index. The scanner page and control room answer.
3. A triggered learner job ran and was recorded; every installed production job is isolated; cancelling a job that is not running was refused.
4. CPU priority on a simulated 1-core PC: the monitor's work time was unchanged with learner and analysis saturated (100 → 99 ms; it doubled before).
5. The System Monitor shows each role's memory and CPU against its limit and the database size, with no page error.

**Note on the first screenshot.** It showed the System Monitor's host tiles empty. A rerun showed the endpoint answering
in 0.2 s and the page fully populated (the screenshot had been taken right after the CPU-pinning test).

**Tests (12 new):**
* isolated runs: separate process, result, failure; a timeout terminates the process; an operator cancel terminates it and is final for the period; a stop interrupts it and it is retried; the cancel endpoint's permissions and refusals;
* the indexes exist and the planner uses the partial index;
* the scanner page and the control room return the same results as before;
* cgroup v1 and v2 reading; thresholds and the CPU rate; the endpoint and the Command Centre alerts.

## 7. Files

**Created**
* `backend/app/services/resources.py`
* `backend/migrations/versions/0011_hot_path_indexes.py`
* `backend/tests/test_resources.py`
* this report

**Modified**
* `backend/app/learning_engine/scheduler.py`: isolated runs; TIMEOUT, CANCELLED and INTERRUPTED; resource reports.
* `backend/app/workers/roles.py`: resource reports.
* `backend/app/api/routes/learning.py`: cancel endpoint.
* `backend/app/api/routes/system.py`: `/api/system/resources`.
* `backend/app/api/routes/trading.py`: critical resource alerts.
* `backend/app/api/routes/ai.py`, `backend/app/api/routes/intel.py`: the two query rewrites.
* `infrastructure/compose.yml`: `cpu_shares`; PostgreSQL `shm_size`.
* `frontend/app/(console)/system/page.tsx`: Role resources.
* `frontend/components/Intel.tsx`: cancel a running job.

**Preserved:** all 215 strategies and what they compute, the Risk Engine, execution, every CPU cap and memory limit,
the scanner scheduling, and every original test. Migration 0011 is additive (indexes only).

## 8. Known limitations

* **Measured on this machine's Docker (Linux, cgroup v1),** not on Docker Desktop for Windows. The settings used (`cpu_shares`, `shm_size`, cgroup v2 reading) are standard Docker and are covered by tests, but the timings will differ on a real PC.
* **The database is not given a CPU cap.** It serves the trading path, so it has high priority instead. A heavy learner query still uses database time; isolated jobs and their timeouts bound how long.
* **Monitoring covers the four roles, the disk and the database size,** not the API, the web interface or Redis individually; their health checks already restart them.
* **History is not pruned.** The indexes keep the hot paths flat, but the database still grows: about 1.2 GB for 40 days of 50 symbols at this rate. A retention policy (for example, agent runs older than 90 days) needs the owner's decision about what history to keep.

## 9. Rollback

* Revert the Phase 20 commit and downgrade migration 0011 (drops the three indexes; no data change).
* The CPU priorities and shared memory revert with the compose file on the next Start All.

## 10. Next phase

**Phase 21 — Security (Part 21)**
* Audit Windows-protected credentials, secure local communication, HMAC and replay protection, and device authentication against what Phases 3–19 built.
* Close the remaining items.
