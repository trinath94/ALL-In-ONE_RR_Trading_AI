# RR Trading One — Phase 17 Report

**Phase:** 17 — Windows user interface (specification Part 17)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 17 asks for a professional desktop application built from the existing Next.js console (not a replacement
dashboard). It lists 27 navigation items and the fields the Command Centre, Multi-Agent Control Room, Broker Symbol
Scanner, Learning Centre and Strategy Library must show.

Phase 17 audited the console against every item, then:
* reorganised the navigation;
* filled every missing field from data the engine already records;
* added the five missing pages;
* built Backup and Recovery in the desktop application, where the backups live.

| Part 17 | Before | Now |
|---|---|---|
| **27 navigation items, in order** | 25 entries in a different order and naming; 5 items had no page (Market Analysis, Trading Worker, Strategy Correlation, Strategy Research Lab, Backup and Recovery) | All 27 in the specification's order and names, each opening its page; Trade Intelligence, Notifications and User Management follow |
| **Command Centre** (23 fields) | Account and P&L figures only | + overall health; backend, database, Redis, MT5 terminal and broker connection status; monitor, analysis, learner and trading-worker status; account mode; symbols discovered and scanned; active strategies; agent activity; latest signals; recent executions; critical alerts (active, and recent events grouped) |
| **Multi-Agent Control Room** (12 fields) | Name, kind, runs, last run | + status, task, current symbol / timeframe, provider / model, last activity, processing time, output, errors, health |
| **Broker Symbol Scanner** (11 fields) | Signal, score, trend, regime, news, levels | + bid / ask and spread (at the scan), strategy consensus, calibrated probability (or "insufficient data"), data quality, last scan, scan priority |
| **Learning Centre** (14 fields) | Trades, wins / losses, verdict, metrics, drift, approvals | + break-even, unattributed trades, training jobs, candidate / validated / production models, learning memory, rollback history |
| **Strategy Library** | ✅ Phase 7: 215 strategies, registry metadata, filters, unavailable and risky flagged, no dummy statistics | Unchanged |
| **Backup and Recovery** | Automatic pre-migration backups only; no list, no restore | **Desktop window**: back up now, list, restore with the typed phrase `RESTORE DATABASE` (section 2). **Console page**: what is protected and how |

| Result | Value |
|---|---|
| End-to-end on the running install | **16 / 17** in the full run; then **all page checks and the outage check passed** on rerun (section 3) |
| Backend suite, including the 215-strategy preservation test | **302 passed** (295 + 7 new) in one full run |
| Desktop core tests (Rust) | **98 passed** (91 + 7 new); clippy clean with warnings as errors |
| Frontend `next build` | Passed, 34 pages (5 new) |
| CI (`461dc3f`) | frontend 43945858386, backend 43945858657, desktop 43945858555: all passed |

## 2. Backup and Recovery

Backups are files on the user's computer, so they belong to the desktop supervisor, which already runs the database
container and takes the pre-migration backups.

* **Back up now:** a new allowlisted action. It writes `manual-<time>.dump` (PostgreSQL custom format); the newest 30 are kept.
* **List:** every backup the supervisor wrote: on request, before a migration, before a restore. Other files in the folder are never listed or touched.
* **Restore**, with the typed phrase `RESTORE DATABASE`, runs a fixed plan:
  1. prerequisites and database up;
  2. a **safety backup** of the current database (`pre-restore-<time>`);
  3. stop everything that uses the database, **trading worker first**;
  4. restore **in one transaction**, so it either fully succeeds or leaves the database unchanged;
  5. apply newer migrations (for an older backup);
  6. **auto trading OFF and reconciliation required**, whatever the backup contained;
  7. restart the engine and the learner. **The trading worker is not restarted.**
* **The action list stays closed.** A restore takes a backup *id*, validated as `manual-` / `pre-migrate-` / `pre-restore-` followed by digits. It is never a path: the supervisor resolves it inside its own backups folder and checks the file is a PostgreSQL dump before copying it into the database container.
* **On failure,** nothing after the failed step runs. The report states that the database is unchanged and to use Start All.

## 3. End-to-end verification

Run on the running install (schema 0010) with the connector, the patched images and the rebuilt desktop supervisor
(the same calls as the desktop window).

**Pages, full run.** All 30 navigation entries opened without a console or page error, the 27 Part 17 items in order,
each titled as in Part 17. Every field listed in section 1 was present on the Command Centre, Control Room, Scanner and
Learning Centre. Market Analysis and Trading Worker showed their data.

**Backup and Recovery, full run, 9 / 9:**
1. **Back up now** wrote a listed `manual-…` PostgreSQL dump.
2. **The database was changed after the backup:** a marker row was added, and auto trading was switched ON directly in the database.
3. **Refusals:** a restore without the phrase and a restore of `../../etc/passwd` were both refused, with nothing changed.
4. **Restore:** all 10 steps OK.
   * The marker was gone (the database was the backup's again).
   * A pre-restore safety backup was taken first.
   * Auto trading was OFF, the trading worker was not running, and the schema was 0010.
   * The monitor reconciled with the broker again before any entry.
5. **Undo:** restoring the safety backup brought the marker back.

**Findings during the runs, all fixed:**
1. **My page check** read the Backup and Recovery page before its data arrived (the screenshot showed the page correct). Fixed in the checker.
2. **A real defect.** A rerun where the connector was not running (my setup) showed that with the MT5 connector offline, the chart-overlays endpoint threw an **unhandled server error (500)**. The tick endpoint had the same gap. The price-data endpoint already answered 502. All three now answer **502 with the reason**: a regression test failed on the old code, and on the install all three answered 502 with the connector stopped.
3. **A real usability defect,** seen in the Command Centre screenshot. Critical alerts listed a kill switch cleared hours earlier and repeated past disconnections as if current, and marked the system "DEGRADED" while everything was up.
   * The panel now separates **active** conditions (kill switch, risk lock, unresolved critical anomaly, service down, pending reconciliation) from **recent critical events (24 h)**, grouped by kind with counts.
   * Only active conditions affect overall health.
   * Regression test added; the rerun screenshot shows "overall healthy", with 20 disconnections and 2 kill switches listed as past events.

**Rerun:** all page checks passed with no errors, and the outage check passed.

## 4. Also fixed: the Phase 16 desktop CI failure

The installer CI's silent-uninstall check failed on the Phase 16 commit with "uninstall left the connector behind".
No desktop code had changed. NSIS runs the uninstaller from a temporary copy that keeps deleting after the launching
process exits; the check waited for the app's `.exe` to disappear but tested the connector folder immediately. It now
waits (up to the same 60 s) for both to be gone, then requires that they are. The check is not weakened: anything
left behind still fails.

## 5. Files

**Created**
* `frontend/app/(console)/{analysis,worker,correlation,research,backup}/page.tsx`
* `frontend/components/SystemStrip.tsx`
* `desktop/ui/backup.js`
* `backend/tests/test_console_overview.py` (7)
* this report

**Modified**
* `backend/app/api/routes/trading.py`: `/api/dashboard/overview`.
* `backend/app/api/routes/intel.py`: scanner fields; `/api/analysis/latest`, `/api/worker/status`, `/api/system/data`; outage handling for tick and overlays.
* `backend/app/api/routes/ai.py`: agent health and markets.
* `backend/app/api/routes/learning.py`: Learning Centre figures.
* `desktop/core/src/{actions,compose,supervisor}.rs`: backup now, list, restore (7 tests).
* `desktop/core/examples/supervise.rs`: `backups`, `restore`.
* `desktop/src-tauri/src/lib.rs`: `backup_list`, `backup_restore`.
* `desktop/ui/{index.html,app.js}`: Backup and Recovery section.
* `frontend/components/Shell.tsx`: navigation.
* Page titles across 14 pages.
* Command Centre, Control Room, Scanner and Learning Centre pages.
* `.github/workflows/desktop.yml`: the uninstall check (section 4).

**Preserved:** all 215 strategies, the engine, the Risk Engine, permissions, every original test and every existing page
(renamed where Part 17 names it differently). No migration.

## 6. Known limitations

* **The desktop window was not exercised in a real Windows session here.** The new Tauri commands are thin wrappers, tested through the supervisor and the `supervise` CLI. The Windows installer build in CI compiles them.
* **Quote values on the scanner are those at the last scan,** labelled so. Live quotes are on the charts.
* **Backups stay on the computer.** The page says to copy the backups folder to external storage; automatic off-machine copies are not implemented.
* **Restore across versions:** an older backup is migrated forward. **Correction (Phase 18):** this was only tested with same-version backups; restoring an older backup failed (safely, rolled back) because `pg_restore --clean` could not drop tables the newer schema still referenced. Fixed and verified in Phase 18 (report section 3). Restoring a backup from a *newer* version than the installed one fails at the migration step, with the database already replaced. The safety backup from step 2 restores the previous state.

## 7. Security implications

* The restore requires a typed phrase, takes only a validated id (never a path), and never restarts the trading worker. It forces auto trading OFF and requires reconciliation afterwards.
* The new console endpoints are read-only and need a signed-in user. The Trading Worker page cannot start the worker; that stays in the desktop window, with the typed phrase.
* No secret is in a backup: keys and passwords stay in the computer's protected storage.

## 8. Rollback

* Revert the Phase 17 commit. No migration.
* Backups written by this phase are ordinary PostgreSQL dumps and stay usable with `pg_restore`.

## 9. Next phase

**Phase 18 — Windows installation structure (Part 18)**
* Audit the installer built earlier, and the first-run flow, against Part 18:
  * the folder layout (program, data and settings; no secrets in the program folder);
  * the first-run wizard steps: dependencies, Docker Desktop and WSL2, database and Redis, MT5 detection, connector, AI provider, administrator, backup, health;
  * clean install and upgrades that keep the data volumes;
  * the verified backup-and-restore path for migrating from the Ubuntu server.
