# RR Trading One — Phase 8 Report

**Phase:** 8 — multi-symbol and multi-timeframe scanning
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The scanner can now handle a broker's whole catalogue and several timeframes without repeating work. Strategies run
only where they apply. Phase 1 gaps 7.1, 7.2 and 7.3 are closed.

| Change | Before | Now |
|---|---|---|
| **When a symbol/timeframe is analysed** | Every 60 s, the same closed bar again and again, each time storing a new analysis and often a new signal and notification | **Once per newly closed bar**, the decision point backtests replicate |
| **Timeframes per strategy** (gap 7.1) | Every strategy ran on every timeframe, whatever it declared | Each strategy runs only on the timeframes it declares, outside the strategy classes (code hashes unchanged). Elsewhere it is counted as *not applicable*, not as "no signal". |
| **Peer data** (gap 7.2) | Never supplied, so the 5 peer strategies could never run | Peers configured per symbol on the Broker Symbols page. Suggestions are offered but never applied automatically. |
| **Scheduling** (gap 7.3) | Flat thread pool, no bound on a cycle, no timeout | Priority, then the longest-waiting job first; a time budget per cycle (overflow deferred, not dropped); per-job timeout; cancellation on stop; a lock so a slow job never runs twice at once |
| **Scanner timeframes** | Environment variable only | Chosen in the console (Market Scanner page), stored in the database (migration 0006), audited. W1 is not offered, because no original strategy declares it. |
| **What happened** | Not recorded | Each analysis stores the data the symbol actually provided and every strategy outcome. Per-day counters feed the Strategy Library; per-cycle reports feed the Market Scanner page. |

| Result | Value |
|---|---|
| Phase 8 end-to-end from a **fresh install** (real stack, analysis service, headless connector with FakeMT5) | **19 / 19 passed** |
| Backend suite, including the 215-strategy preservation test | **223 passed** (210 + 13 new) |
| Frontend `next build` (strict TS) | Passed, 25 pages |

## 2. What was built

### 2.1 Scheduler (`backend/app/scanner/scheduler.py`, new; used by `scanner/service.py: scan_once`)

**Due jobs.** A job (symbol, timeframe) is due when the broker reports a closed bar newer than the last one analysed.
* The check is a 3-bar request using the same closed-bar rule as the analysis.
* The marker is written after the analysis is committed, so a crash leaves the bar due and it is retried, never lost.
* A failed probe means "due", so the analysis reports the problem instead of the job silently never running.

**Order.** Symbol priority first, then the oldest pending bar, then the higher timeframe. Jobs deferred by the budget
therefore go first next cycle, so no symbol starves.

**Budget.** Jobs start only while `RR_SCAN_CYCLE_BUDGET_SECONDS` (default 50, below the 60 s interval) lasts; the rest
stay due.

**Job timeout.** After `RR_SCAN_JOB_TIMEOUT_SECONDS` (default 45) a job is abandoned and reported, freeing its worker
slot. A Redis in-flight lock keeps an abandoned job from being started again while it still runs.

**Stop.** A stop request to the analysis service cancels the jobs not yet started.

**Reports.** Each cycle's report (jobs, due, not due, analysed, deferred, timed out, errors, duration, per timeframe)
goes to Redis, with a 50-cycle history.

**Switches.** `RR_SCAN_ONCE_PER_BAR` and `RR_SCAN_ENFORCE_TIMEFRAMES` (both default `true`) can restore the original
behaviour.

### 2.2 Analysis changes (`scanner/service.py: analyse`)

**Applicable strategies.** The enabled set is split into strategies that declare the job's timeframe and those that
don't. Only the first group is evaluated and passed to the signal engine and the Part 26 intelligence.

**Peers.** For symbols with configured peers, and only when a peer strategy applies:
* the peers' closed bars are fetched, for peers the same broker still offers;
* `peer_symbols` is added to the data capabilities;
* problems are recorded (for example, "not offered by the broker").

**Recorded on every analysis**
* `data_caps`, `peers` and `peer_problems`;
* `evaluation`: counts of signal, no signal, unavailable, error and not applicable;
* the list of unavailable strategies.

Per-strategy outcome counters are kept per day in Redis.

### 2.3 Database (migration `0006`, additive)

* `symbols.peers` (JSONB, default `[]`).
* `scanner_settings`: a single row holding the timeframes, plus who changed them and when.

The downgrade drops exactly these.

### 2.4 API (`api/routes/intel.py`, additive)

| Route | Permission | Purpose |
|---|---|---|
| `GET /api/scanner/status` | any user | Settings, applicable strategies per timeframe, last cycle, history, and the last analysed bar per job |
| `PUT /api/scanner/settings` | `strategy.manage` | Choose timeframes (M1–D1). Audited. |
| `PATCH /api/symbols/{id}` `peers` | `strategy.manage` | Up to 3 peers, which must be in the same broker's catalogue and not the symbol itself |
| `POST /api/symbols/peers/apply-suggested` | `strategy.manage` | Explicitly apply suggestions to symbols with no peers. Audited. |

**Read-side additions**
* `GET /api/symbols` adds `peers` and `suggested_peers`.
* The Strategy Library adds per-strategy `evaluations`.
* The library summary reports timeframe enforcement as it actually is.

**Peer suggestions** (`scanner/peers.py`)
* They come from a short list of instrument groups that usually move together, such as EURUSD/GBPUSD and XAUUSD/XAGUSD.
* They are limited to what the broker offers.
* Inverse pairs are never suggested.
* They are labelled as suggestions for the user to check.

### 2.5 Console

**Market Scanner**
* A new "Scanner schedule" panel: timeframe chooser showing how many enabled strategies apply on each, the cadence, budget and timeout, and the enforcement and once-per-bar rules in plain words.
* A "Last cycle" panel: analysed, not due, deferred, timed out, errors and duration against the budget; error samples; recent cycles.

**Broker Symbols:** a Peers column (editable for managers, suggestions shown as placeholders) and "Apply suggested
peers". **Fixed:** the search, class and view filters never appeared, the same untitled-panel bug fixed in the library
in Phase 7.

**Strategy Library**
* Per-strategy "Evaluated (7 d)": signal, no signal, data unavailable, not applicable, errors.
* The timeframe note now reflects enforcement.

## 3. Verification

**Unit and integration tests** (`tests/test_scanner_scheduling.py`, 13): due rule; ordering; budget deferral; stop
cancellation; slow-job abandonment; once-per-bar through the bridge (same bars give 0 new analyses, a new bar gives a
full cycle); timeframe enforcement; peers supplied, and a peer the broker no longer offers reported and not used;
in-flight lock; settings and status API; peer validation and explicit suggestions; library evaluations.

**End-to-end from a fresh install, the running analysis service (19 / 19)**
* Schema at `0006`; Phase 5 setup; both fake symbols enabled for scanning.
* **First cycle: 6 analyses** (2 symbols × M15, H1, H4). **Two minutes later: still 6.** The latest cycle reported 6 jobs not due, 0 analysed, 0 errors.
* M15 analyses count not-applicable strategies. The library reports enforcement on and evaluation counts recorded. `ict_smt` was unavailable 6 times without peers (3 timeframes × 2 symbols).
* A symbol as its own peer was refused (422). After configuring `XAUUSD.r` as `EURUSD.r`'s peer and simulating a new H1 bar for EURUSD.r, **only that job was re-analysed**. **Its analysis received the peer:** `peers=["XAUUSD.r"]`, `peer_symbols` in the data, SMT no longer unavailable.
* W1 refused (422); timeframes set to H1 only; **the analysis service's next cycle had exactly 2 jobs**; the change was audited.
* Console, logged in as the administrator, with no console errors:
  * the Market Scanner schedule (H1 selected, strategies per timeframe) and last-cycle panels;
  * Broker Symbols with visible filters and the peer `XAUUSD.r` in the Peers column;
  * the Strategy Library detail for ICT SMT showing its 7-day evaluations.

*Test method note:* FakeMT5 only produces a new bar on the hour, so the e2e simulated "the broker closed a new H1 bar"
by removing that job's last-bar marker. The unit test covers the real path by shifting the fake feed's bar times.

## 4. Findings

| # | Finding | Outcome |
|---|---|---|
| 1 | **The original scanner re-analysed the same closed bar every minute.** It stored a new analysis and often a new signal, plus A+/BUY/SELL notifications, for an unchanged decision point. The Risk Engine's one-position-per-symbol rule limited the execution impact, but signal, notification and database volume grew about 60× per bar on M15–H4. | Once per closed bar (tested, and verified on the running service). |
| 2 | Strategies ran on timeframes they don't declare (gap 7.1). | Enforced outside the strategy classes; counted as not applicable. |
| 3 | The 5 peer strategies could never run (gap 7.2), although the original seed marks them available. | Configured peers supply them (tested, and verified on the running service). The library now classes them "per symbol". |
| 4 | No bound on a scan cycle, and no timeout (gap 7.3). | Budget, deferral, timeout, lock and cancellation (tested with controlled slow jobs). |
| 5 | Broker Symbols page filters invisible (my Phase 6 page). | Fixed and covered by the page check. |
| 6 | A local Redis dump file was committed in the first Phase 8 work-in-progress commit. It came from the sandbox's test Redis and contained only test data, no real secrets. | Removed in the next commit, `dump.rdb` ignored, and the test Redis now writes elsewhere. It remains in that one commit's history. |

## 5. Files

**Created**
* `backend/app/scanner/scheduler.py`
* `backend/app/scanner/peers.py`
* `backend/migrations/versions/0006_scanner_scheduling.py`
* `backend/tests/test_scanner_scheduling.py` (13 tests)
* this report

**Modified**
* `backend/app/scanner/service.py`: scheduler, applicability, peers, outcome recording
* `backend/app/workers/roles.py`: stop request passed to the scan
* `backend/app/core/config.py`: four scheduling settings
* `backend/app/db/models.py`: `Symbol.peers`, `ScannerSettings`
* `backend/app/api/routes/intel.py`
* `backend/app/services/strategy_library.py`
* `backend/tests/test_strategy_library.py`: the peer strategies are now "per symbol" and enforcement is on. These assertions changed because the behaviour changed; none was weakened.
* `frontend/app/(console)/{scanner,symbols,library}/page.tsx`
* `.gitignore`

**Preserved:** all 215 strategies (no strategy class or registry entry touched; the preservation test passes) and every
original test.

## 6. Known limitations

* **Real broker catalogues are still not exercised.** FakeMT5 offers 2 symbols and only hourly bars. The scheduler's budget, timeout and fairness are tested with controlled jobs, not with hundreds of real symbols. Each due check costs one 3-bar request per job and cycle; at several hundred symbols that is the next thing to measure on a real terminal.
* **Peer correlation is the user's call.** Suggestions are labelled, and nothing measures the correlation yet. A measured correlation belongs with the Phase 10 historical data work.
* **An abandoned (timed-out) job's thread is not killed.** Python can't kill threads. Its slot is freed and the lock prevents a duplicate, but a truly hung terminal request still holds a thread until its own HTTP timeout.
* **Evaluation counters live in Redis.** They are lost if Redis data is lost, and the library then counts from zero, which it states. The analysis records in PostgreSQL keep the per-analysis totals.
* **The Claude review still receives `spread: 0.0`** (gap 7.13); that is scheduled for Phase 13.
* No real Windows or MetaTrader 5 (unchanged).

## 7. Security implications

* The new write routes require `strategy.manage`, the same as the existing symbol and strategy controls, and are audited. Read-only users and traders are refused (tested).
* Nothing here can trade. Analysing fewer duplicate bars *reduces* the number of signals the trading worker could consider; every execution gate is unchanged.
* Peers can only name symbols in the same broker's catalogue, so no arbitrary symbol names reach the connector.

## 8. Rollback

* Revert the Phase 8 commits. `alembic downgrade 0005` drops only `symbols.peers` and `scanner_settings`.
* **Without reverting:**
  * `RR_SCAN_ONCE_PER_BAR=false` restores per-minute re-analysis;
  * `RR_SCAN_ENFORCE_TIMEFRAMES=false` runs every strategy on every timeframe.

## 9. Next phase

**Phase 9 — multi-agent orchestration**
* An AI provider gateway (gap 7.15) that keeps the existing `enforce_boundaries` contract for every provider, plus a usage and cost ledger.
* Coordination of the existing specialist agents.
* AI still cannot enable LIVE, change permissions or run commands.
