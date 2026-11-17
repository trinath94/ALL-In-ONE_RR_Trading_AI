# RR Trading One — Phase 23 Report

**Phase:** 23 — Migration from the existing Ubuntu deployment (specification Part 23)
**Date:** 2026-11-17
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 23 asks for a migration utility that backs up, verifies, restores and migrates the server's database. It must
then verify strategy, account, trade, closed-position, performance, experiment, learning, model and configuration
records and the row counts, and produce a migration report. Three rules apply:
* never copy a Docker volume;
* never overwrite local records without an explicit policy;
* keep the Ubuntu server available for rollback.

Phase 18 built the verified import: scratch restore, safety backup, single-transaction restore, migrations, "no row
lost", reset to DEMO. Phase 23 completed it into the migration utility Part 23 describes.

| Part 23 item | Before | Now |
|---|---|---|
| 1. Back up PostgreSQL | Server `backup.sh`; safety backup of the PC | **+ `scripts/migrate_export.sh`**: a read-only server export whose dump and per-table row counts come from **one database snapshot**, plus a manifest (SHA-256, size, schema, PostgreSQL version, counts) |
| 2. Verify backup integrity | Scratch restore | **+ manifest:** the file is refused on arrival if its SHA-256 or size differ; the restored rows and schema must equal the manifest exactly |
| 3. Restore to local PostgreSQL | ✅ (one transaction, Phase 18/21) | Unchanged |
| 4. Apply Alembic migrations | ✅ | Unchanged |
| 5–13. Verify strategy, account, trade history, closed positions, performance, experiments, learning, model metadata, configuration | Row counts only | **Content verification:** every table's content is fingerprinted in the scratch database (over the server's own columns and rows). After the migration the same rows must give the same fingerprint. Results are grouped into these nine domains. |
| 14. Verify row counts | ✅ | Part of the per-table results |
| 15. Produce a migration report | Plan steps only | **Migration report** (Markdown, kept next to the import, shown in the window, `supervise import-report`). Written even when an import fails, saying how far it got. |
| Never copy a Docker volume | ✅ (logical dump only) | Stated in the export and the runbook; a non-dump file is refused |
| Never overwrite local records without an explicit policy | Typed phrase for any import | **Explicit REPLACE policy:** if the PC holds trading records (or they cannot be checked), the import stops before changing anything unless `IMPORT AND REPLACE LOCAL DATA` is typed. The report states the policy and what the PC held. |
| Keep Ubuntu available for rollback | Import never touched the server | Export is read-only (verified); the report and runbook name both rollbacks (back to the server; the PC's safety backup) |

| Result | Value |
|---|---|
| End-to-end: server export under write load → damaged transfer → policy refusal → REPLACE import → report → undo | **12 / 12** (section 4) |
| Desktop core tests (Rust) | **130 passed** (120 + 10 new); clippy clean |
| Engine | unchanged in this phase |

## 2. How content is verified

* **Tables with an integer `id`** (nearly all): one MD5 over every row, using the server's columns and ordered by id, plus the server's largest id. After the restore and migrations, the same columns of the rows up to that id must give the same MD5. Rows this version adds afterwards (215 strategies, new permissions, settings) are allowed and counted.
* **Tables without one** (link tables): the MD5 of every row, each of which must still be present. An empty table is recorded as empty, not missing.
* **Changes made on purpose,** listed rather than flagged:
  * the schema version;
  * the standard roles' permissions, which the seed resets to this version's.
* **Safety of the generated SQL.** Table and column names come from the database's catalog and are accepted only as plain lowercase identifiers. Anything else is reported as not checked and never put into SQL. The script runs through psql's stdin.

## 3. Findings

* **A real defect, found by the end-to-end run.** The first version treated an empty table without an `id` as missing ("could not be fingerprinted"). The unit-test fake had only tables with ids. Fixed (an empty link table is recorded as having no rows), with a test.
* **Snapshot consistency, verified.** The export ran while another session kept inserting rows on the server. The dump held exactly the manifest's counts (479 rows), and the PC's check passed.

## 4. End-to-end verification

Run on the running install, which holds its own trading records (trades, closed positions, models). The server was a
PostgreSQL 16.6 at schema 0003 with users, accounts (one LIVE, approved), 240 closed positions, signals, the server's
connector registration and sessions.

1. **Install running:** schema 0011; the owner signs in; the PC holds trading records.
2. **Export while the server was writing:** dump and manifest (schema 0003, 240 closed positions). The server kept running and writing, and its data and content were identical afterwards.
3. **A copy with one byte changed** was refused against its manifest; nothing was added.
4. **The intact copy** was added, encrypted, with its manifest.
5. **`IMPORT DATABASE`** was refused at verification ("this PC already holds trading records"). Nothing was stopped or replaced, and a report recorded it ("not reached").
6. **`IMPORT AND REPLACE LOCAL DATA`:** all steps OK.
   * The manifest matched.
   * Content verified: 61 tables; 53 unchanged and 7 with every server row unchanged plus this version's additions.
   * Schema 0011; made safe; engine back.
7. **The report:** all 15 items PASS.
   * Closed positions: 240 server rows, content unchanged; accounts, trade history and configuration verified.
   * Policy REPLACE, with this PC's records before.
   * The safety backup to undo; LIVE → DEMO; server untouched.
8. **The Ubuntu administrator** signed in on the PC; the trading worker was not running.
9. **Undo:** the safety backup named in the report brought back the PC's own database and records. Reconciliation passed again.

**How the runs went:** the first run passed 6 / 12 because of the defect in section 3. The rerun passed 12 / 12.

## 5. Files

**Created**
* `scripts/migrate_export.sh`
* `desktop/core/src/migration.rs`
* `docs/MIGRATION_FROM_UBUNTU.md` (runbook)
* this report

**Modified**
* `desktop/core/src/supervisor.rs`: manifest check, policy gate, fingerprints, content comparison, report, `migration_report`.
* `desktop/core/src/actions.rs`: `REPLACE_PHRASE`, `VerifyDump { replace_local }`, `WriteMigrationReport`.
* `desktop/core/src/compose.rs`: stdin psql prints plain rows.
* `desktop/core/src/lib.rs`: module.
* `desktop/core/examples/supervise.rs`: `import-report`.
* `desktop/src-tauri/{src/lib.rs,build.rs,capabilities/main.json}`: `backup_import_report` (declared and granted).
* `desktop/ui/{backup.js,index.html}`: the report after an import; export instructions; the two phrases.

**Preserved:** the server (never written to), the engine, every test.

## 6. Known limitations

* **The report's time** is a Unix timestamp; the manifest carries the export's ISO date.
* **Content verification compares this version's view of the server's rows.** A migration that changed existing values would be reported as changed. None of 0004–0011 does: they only add.
* **Large databases:** fingerprinting reads every row once in the scratch database and once after the migration. This took seconds here; a very large history adds minutes, not hours.
* **The export script** was run against a PostgreSQL container directly (`RR_PG_CONTAINER`). On the server it uses the deployment's `docker compose`, as `backup.sh` does.

## 7. Rollback

* Revert the Phase 23 commit. Imports then use the Phase 18 checks; the server export remains a plain dump usable by the earlier import (without the manifest check).
* For a migration already done, the report names the safety backup that restores the PC's previous database. The server was never changed.

## 8. Next phase

**Phase 24/25 — Development phases and required deliverables (Parts 24–26).** Audit the deliverables list against
what exists, assemble what is missing (documentation set, handover), then the final installer.
