# RR Trading One — Phase 18 Report

**Phase:** 18 — Windows installation structure (specification Part 18)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 18 asks for four things:
* a fixed folder layout for the program, its data and its settings;
* a first-run flow from dependencies to a verified, healthy system;
* clean installs and upgrades that never remove the PostgreSQL or Redis data;
* migration from the Ubuntu server by a **verified** PostgreSQL backup and restore.

The installer and supervisor built in Phases 3–8 already covered most of the layout and the upgrade rules. Phase 18
audited them item by item and built the two parts that were missing:
1. **the verified Ubuntu import**;
2. **the first-run wizard**.

The import's end-to-end test found a real defect in the Phase 17 restore. It is fixed (section 3).

| Part 18 item | Status | Where / evidence |
|---|---|---|
| Program in `C:\Program Files\RR Trading One` (desktop, backend, frontend, mt5-connector, infrastructure) | ✅ Phase 8 | NSIS `perMachine`. Bundle resources: `backend/`, `frontend/`, `infrastructure/`, `docker/`, `mt5-connector/`. The desktop `.exe` is at the install root. Windows CI installs and checks the layout. |
| `updater/` | ❌ **Not built** | Needs release hosting and a code-signing decision (section 6). Upgrades today: run the newer installer. |
| Data in `C:\ProgramData\RR Trading One` (backups, logs, secrets) | ✅ | `paths.rs` (tested). Backups, logs and secrets are there. |
| Database, models, datasets, cache | ✅ Different place, by design | They are in the Docker named volumes `rr-trading-one_pgdata` and `rr-trading-one_redisdata` (design decision D2, Phase 3). Models and datasets are database rows (HMAC-signed artefacts since Phase 11). A volume is the only place PostgreSQL runs safely under Docker Desktop on Windows. |
| Settings in `%APPDATA%\RR Trading One`; no secrets in the program folder | ✅ | `paths.rs`. Secrets are protected with Windows DPAPI (Credential Manager, Phase 3) and written to ProgramData `secrets\`, never to the install folder. |
| **First-run wizard** (dependencies, Docker Desktop and WSL2, PostgreSQL and Redis, backend and frontend, MT5 detection, connector, AI provider, administrator, backup, health) | ✅ **New** | A card at the top of the desktop window (section 4). |
| Docker Desktop install | ✅ Phase 8 | The installer *asks*, then uses winget. It is never silent. |
| WSL2 validation | ✅ Phase 3 | `wsl.exe --status` is a prerequisite on Windows. |
| Clean install and upgrade; never remove the PostgreSQL / Redis volumes | ✅ | The supervisor's compose commands cannot contain `down`, `rm`, `-v`, `--volumes`, `prune` or `kill` (tested). Before install and uninstall the hooks only *stop* services. Windows CI checks that an uninstall leaves the volumes. Migrations run after an automatic pre-migration backup. |
| **Migration from Ubuntu by verified backup and restore** | ✅ **New** | Section 2. End-to-end **16 / 16** on the running install. |

| Result | Value |
|---|---|
| Ubuntu import end-to-end on the running install | **16 / 16** (section 5) |
| Desktop core tests (Rust) | **104 passed** (98 + 6 new); clippy clean with warnings as errors |
| Backend suite, including the 215-strategy preservation test | **302 passed** in one full run (one assertion added to an existing test) |
| Desktop window in a browser with a mocked supervisor | wizard and import flow passed; earlier window check passed unchanged |
| CI for Phase 17 (`461dc3f`) | frontend 43945858386, backend 43945858657, desktop 43945858555: **all passed** |

## 2. Migrating from the Ubuntu server

On the server, `scripts/backup.sh` writes `backups/rr_<stamp>.dump` (PostgreSQL custom format). The user copies that file
to the PC, then in the desktop window goes to **Backup and Recovery → Import from the previous Ubuntu server**.

**Adding the file** (`backup_import_file`):
* The file must exist and be at most 50 GB.
* It must start with the PostgreSQL custom-format header (`PGDMP`).
* It is **copied** into the backups folder as `import-<time>.dump`. The original is never touched.
* From then on, only that id is used, never a path.

**Importing it** needs the typed phrase **`IMPORT DATABASE`**; the restore phrase is refused. It runs a fixed plan:
1. prerequisites; database up;
2. **verify the dump in a scratch database** (`rr_import_check`):
   * it is restored there with `--exit-on-error` and every table is counted;
   * if it does not restore cleanly, **nothing is replaced** and the plan stops;
   * the scratch database is dropped either way;
3. **safety backup** of the installation's current database (`pre-restore-<time>`);
4. stop everything that uses the database, **trading worker first**;
5. **restore in one transaction** (section 3);
6. migrations from the server's schema to this version's (with their own pre-migration backup);
7. **verify again: no row lost.** Every table of the verified dump must be present with at least its row count. Rows added by this version's migrations and seed (the 215 strategies, new permissions) are listed, not hidden;
8. **start like a new installation**:
   * DEMO, auto trading OFF;
   * LIVE approvals cleared and LIVE accounts inactive;
   * reconciliation required;
   * audited as `DATABASE_IMPORTED`.
   Whatever the server was doing (in the test it was LIVE with auto trading ON), the PC starts in DEMO;
9. start the engine and the learner. **The trading worker is not started.**

**What the user does afterwards** (stated in the window):
* sign in with the server's user accounts;
* link the MT5 connector on this PC;
* re-enter AI provider keys and broker passwords: they were encrypted with the server's keys, which never leave it;
* retrain models: the server's were unsigned (signing came in Phase 11), and unsigned models cannot be used.

## 3. A defect found and fixed: restoring an older backup

The first import run failed at the restore step, with the database left unchanged (the transaction rolled back).

* **Cause:** `pg_restore --clean` drops only the objects the *backup* knows about. The server's schema-0003 backup did not know the newer tables, which still referenced `users`, so the drop failed.
* **Also affected:** the same code path is the Phase 17 restore. Phase 17 said an older backup "is migrated forward", but that was only tested with same-version backups. **That claim was wrong until now.**
* **Fix:** the restore now runs in two steps:
  1. `pg_restore` converts the whole dump to SQL first, so a damaged file fails before the database is touched;
  2. `psql --single-transaction` replaces the `public` schema and applies that SQL **in the same transaction**: all or nothing.
* **Verified on the install:** the schema-0003 dump restored over the schema-0010 database and migrated forward, and same-version restores (the undo) still work.
* **Tests:** the restore tests now require that the dump is read in full before the database is touched, and that the schema reset is in the single transaction.

## 4. First-run wizard

A card at the top of the desktop window lists the nine steps of Part 18, in order. Each step is **read from the running
system every time, never stored as "done"**:

| Step | Done when |
|---|---|
| 1. Windows, Docker Desktop, WSL2, disk, port | every prerequisite check passes |
| 2. Engine started | postgres, redis, api, frontend and gateway are running |
| 3. Health | the engine and web health probes are healthy |
| 4. Administrator | the engine reports an administrator exists |
| 5. MT5 terminal | a terminal profile is saved |
| 6. Connector | the connector is running and the engine sees it online |
| 7. DEMO account | the active account is a verified DEMO account |
| 8. AI provider (**optional**) | at least one AI task is configured (new `ai_configured` in the local status API) |
| 9. First backup | a backup exists |

* The card names the next step and the section of the window that holds its button. The buttons themselves stay in their sections, so the wizard adds no new way to change anything.
* Supervisor calls run one at a time, because the supervisor refuses concurrent actions.
* **Defect found by the browser check:** the wizard read `service`/`state`, but the supervisor returns `Service`/`State`, so step 2 could never show as done. Fixed before commit.
* **Browser check (mocked supervisor):**
  * a fresh PC shows step 1 as next;
  * a set-up PC without a backup shows "Take a first backup";
  * after the import flow the card reads "Setup is complete";
  * text from the supervisor is never rendered as HTML;
  * the layout does not overflow at 900 px.

## 5. Verification

**Ubuntu import, on the running install, through the desktop supervisor (16 / 16).**

The source was a real PostgreSQL 16.6 at schema **0003** (the server's version), holding:
* 2 users with argon2 passwords;
* `trading_state` LIVE with auto trading ON;
* a DEMO account and an **approved, active LIVE** account;
* 240 closed trades, 120 signals, 75 audit rows.

It was dumped with the same command as `scripts/backup.sh`.

1. Install running: schema 0010; its owner can sign in.
2. A non-dump file and a missing file were refused. The dump was copied and listed as "from the Ubuntu server".
3. Refused, with nothing changed:
   * import without the phrase;
   * import with the restore phrase;
   * the import id given to the plain restore;
   * a path instead of an id.
4. **A truncated dump** failed verification in the scratch database. The installation's database, engine and owner were untouched, and the scratch database was dropped.
5. **The import:** all 12 steps OK.
   * Schema 0003 → 0010.
   * No row lost across 61 tables (users 2, trades 240, signals 120).
   * A safety backup was taken first.
   * DEMO, auto trading OFF, the LIVE approval cleared and the LIVE account inactive, audited.
   * Trading worker not running.
   * The Ubuntu administrator signed in with the Ubuntu password; the installation's owner no longer existed.
6. Connector relinked and the DEMO account registered. The engine reconciled with the broker (startup checks passed) in DEMO.
7. **Undo:** restoring the safety backup brought back the installation's own database (owner signs in, no Ubuntu users). Relinked; reconciliation passed again.

The first run passed 12 / 16; the four failures were the restore defect in section 3. Then the script needed one fix:
it relinked while the connector was running, which the supervisor correctly refuses. After both fixes, 16 / 16, and
16 / 16 again after the final change (`psql --set` instead of `-v`).

**Tests:**
* Rust:
  * import copies only PostgreSQL dumps;
  * import verifies in a scratch database before anything is replaced;
  * an unrestorable dump never touches the real database;
  * a lost row fails the import, added reference data does not;
  * the import plan's order and phrase;
  * database commands run only inside the postgres container, with no volume-removing flag.
* Backend: the local status reports `ai_configured`.

## 6. Known limitations

* **Updater not built.** Part 18 lists `updater/`. A self-updater needs:
  * somewhere to host releases;
  * a code-signing certificate, so updates can be verified and Windows SmartScreen accepts them.

  Both are the owner's decision. Until then, an upgrade is running the newer installer: it stops the services, keeps every volume and setting, and the next Start All migrates after an automatic backup.
* **Secrets do not migrate,** by design: the server's encryption keys never leave it. AI keys and broker passwords are re-entered on the PC.
* **The import replaces the whole database,** including the PC's own users; it is meant for a new installation. The safety backup undoes it.
* **Disk:** the import needs room for the dump, its SQL form in the container and the safety backup, about three times the dump size.
* **The desktop window was not run in a real Windows session here.** The new Tauri commands are thin wrappers over the supervisor, which was exercised through the `supervise` CLI. The Windows installer build in CI compiles them.

## 7. Security implications

* The import takes a validated id, never a path, after the file has been copied and its header checked. It needs its own typed phrase.
* An imported database **can never come up trading**: DEMO, auto trading OFF, LIVE approvals cleared, reconciliation required, trading worker not started. Re-enabling LIVE goes through the full Phase 16 process.
* No secret is in a backup or an import.
* The SQL the supervisor runs is fixed constants; nothing from the dump or the user is put into a command.

## 8. Rollback

* Revert the Phase 18 commit. No migration.
* Backups and imports are ordinary PostgreSQL dumps, usable with `pg_restore`.
* After an import, restoring its `pre-restore-…` backup returns the installation to its previous state.

## 9. Files

**Created**
* `desktop/ui/wizard.js`
* this report

**Modified**
* `desktop/core/src/actions.rs`: `IMPORT DATABASE`, import plan, steps, `import-` ids (restore refuses them).
* `desktop/core/src/compose.rs`: scratch database, row counts, post-import reset, restore in one transaction with schema reset.
* `desktop/core/src/supervisor.rs`: `import_dump`, `import`, dump verification, "no row lost" check.
* `desktop/core/examples/supervise.rs`: `import-file`, `import`.
* `desktop/src-tauri/src/lib.rs`: `backup_import_file`, `backup_import`, `prerequisites`.
* `desktop/ui/{index.html,app.js,backup.js,style.css}`: wizard card, import section.
* `backend/app/api/routes/local.py`, `backend/tests/test_local_api.py`: `ai_configured`.

**Preserved:** all 215 strategies, the engine, the Risk Engine, permissions and every original test. No migration.

## 10. Next phase

**Phase 19 — local and remote architecture (Part 19)**
