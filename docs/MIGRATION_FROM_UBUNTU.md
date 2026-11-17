# Moving from the Ubuntu server to RR Trading One on Windows

This runbook moves the trading database of the existing Ubuntu deployment to RR Trading One on a Windows PC. The
server is never changed, and stays your rollback until you accept the PC.

**Never copy a Docker volume** from the server to Windows. A volume is tied to the server's PostgreSQL build and
operating system. The migration uses a logical, verifiable PostgreSQL export instead.

## 1. Before you start

* Install RR Trading One on the PC and complete the first-run wizard (Start All, administrator).
* On the server, **switch auto trading OFF** in the console. The server keeps running; it just stops opening trades.
* If the server's connector runs on the same PC as the new installation, close that standalone connector and disable its start-up (the PC's wizard warns about it).

## 2. Export on the server (read-only)

In the deployment folder on the server:

```bash
scripts/migrate_export.sh
```

It writes, in `backups/`:
* `rr_migrate_<stamp>.dump`: the database, as a PostgreSQL custom-format dump.
* `rr_migrate_<stamp>.manifest.json`: the dump's SHA-256 and size, the schema version, the PostgreSQL version and the row count of every table.

The row counts and the dump come from one database snapshot, so they agree even while the server is working. The
script changes nothing on the server.

## 3. Import on the PC

1. Copy **both files** to the PC, into the same folder.
2. In RR Trading One, open **Backup and Recovery → Import from the previous Ubuntu server**.
3. Enter the path of the `.dump` file and choose **Add dump**. The PC checks the file against its manifest (SHA-256 and size); a file damaged in transfer is refused here. The dump is stored encrypted.
4. Type the phrase and choose **Import**:
   * `IMPORT DATABASE` when this PC has no trading records yet (a fresh installation);
   * `IMPORT AND REPLACE LOCAL DATA` when it does. This is the explicit policy to replace them; without it the import stops before changing anything.

What the import does, in order:
1. **Integrity:** restores the dump into a scratch database. The rows must equal the manifest's counts and schema version exactly. Every table's content is fingerprinted.
2. **Safety backup** of the PC's current database.
3. **Restore**, in one transaction.
4. **Migrations** from the server's schema to this version's.
5. **Content verification.** Every row the server had must be present and unchanged; rows this version adds (new strategies, permissions, settings) are listed.
6. **Start like a new installation:**
   * DEMO and auto trading OFF;
   * LIVE approvals cleared;
   * the server's connectors, link tokens and sessions revoked;
   * reconciliation with the broker required before any entry.
7. **The migration report.**

## 4. Read the migration report

It is shown in the window after the import, and kept as `<import id>.migration-report.md` in the backups folder.
It contains:
* **The Part 23 checklist:** backup, integrity, restore, migrations; strategy, account, trade-history, closed-position, performance, experiment, learning, model and configuration records; row counts. Each is PASS or FAIL, with numbers.
* **The source:** dump, SHA-256, schema and PostgreSQL version.
* **The policy:** whether this PC's records were replaced, and what they were.
* **The safety backup** that undoes the import.
* **What was changed on purpose** (DEMO, LIVE approvals, server identities).
* **Every table:** server rows, rows now, result.

## 5. After the import

* Sign in with the server's user accounts.
* **MT5 Manager:** link the connector, then use the account as the DEMO account.
* Re-enter AI provider keys and broker passwords. They were encrypted with the server's keys, which never leave the server.
* Retrain models: models from the server are unsigned here and cannot be used.
* Re-enabling LIVE goes through the full approval process again.

## 6. Accepting, or rolling back

* **To accept:** after you have traded in DEMO on the PC and are satisfied, decommission the server. Until then, do not delete it.
* **Rollback, back to the server:** the server was never changed. Switch its auto trading back on and stop the PC's engine (Stop All).
* **Rollback, the PC's previous database:** in Backup and Recovery, restore the safety backup named in the report, with `RESTORE DATABASE`.
