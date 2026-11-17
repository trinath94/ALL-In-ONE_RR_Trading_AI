# RR Trading One: recovery manual

**First rule: the broker is the source of truth.** Stop-losses and take-profits live at the broker, so open
positions stay protected even when the PC, Docker or the engine is down. After any recovery, the engine reconciles
with the broker before it allows a new entry.

## 1. Backups

| Kind | When | Kept |
|---|---|---|
| `manual-…` | Backup and Recovery → **Back up now** (also before a secret rotation) | newest 30 |
| `pre-migrate-…` | automatically before every database migration (Start All after an upgrade) | newest 10 |
| `pre-restore-…` | automatically before every restore or import | newest 10 |
| `import-…` | an Ubuntu dump you added, encrypted, with its manifest and migration report | until removed |

* **Where:** `C:\ProgramData\RR Trading One\backups`, encrypted (`.rrbk`). Backups made before encryption existed (`.dump`) are still listed and restorable.
* **The backup key:** Security → **Show backup key** (`SHOW BACKUP KEY`), `RRBK1-…`. **Store it offline, away from the backups.** Without it, the backups cannot be restored on another PC.
* **Copy the backups folder off the PC regularly** (external disk, another machine). The application does not do this for you.

## 2. Restore a backup

Backup and Recovery → choose the backup → **Restore…** → type `RESTORE DATABASE`. The plan:
1. a safety backup of the current database;
2. everything that uses the database is stopped, trading worker first;
3. restore in **one transaction**: it either fully succeeds or changes nothing;
4. migrations, for an older backup;
5. **auto trading OFF** and reconciliation required;
6. the engine restarts. **The trading worker is not restarted.**

Each step's result is shown. If a step fails, nothing after it runs, and the report says whether the database is
unchanged. Use **Start All** to bring the engine back.

**Undo a restore:** restore the `pre-restore-…` backup it took first.

## 3. A new PC (disk failure, replacement)

1. Install RR Trading One, then run Start All and create an administrator (Installation manual).
2. Security → **Add backup key**: paste the old installation's `RRBK1-…` key.
3. Copy the old backups folder's `.rrbk` files into the new backups folder.
4. Restore the newest backup (`RESTORE DATABASE`).
5. Relink the MT5 connector (MT5 Manager), then re-enter the AI provider keys and broker passwords. These are encrypted with the old installation's keys, which were not exported.

A backup encrypted with a key this installation does not hold is refused **before anything is stopped**, with the id
of the key it needs.

## 4. Common situations

| Situation | What to do |
|---|---|
| A service is down or unhealthy | Service manager → **Recovery** (restarts unhealthy services; never the trading worker), or Start All |
| Docker Desktop was not running | Start Docker Desktop, then Start All |
| The connector shows NOT_REGISTERED | MT5 Manager → Stop connector → Link connector → Start connector |
| The connector shows a wrong account | Log the terminal in to the profile's account; the connector refuses to trade a different one |
| Kill switch active | Find the reason (Command Centre / Risk Centre), fix it, then reset the kill switch (trading control permission) |
| "Reconciliation pending" | Wait for the monitor's startup checks (connector connected); entries stay blocked until they pass |
| A role near its memory limit, or low disk | System Monitor shows which. Free disk space: old backups after copying them away, Docker build cache. |
| A learning job running too long | AI Learning Centre → cancel (the job's process is terminated) |
| A wrong secret, or everyone must be signed out | Security → **Rotate secrets** |
| An upgrade's migration failed | The `pre-migrate-…` backup was taken first. Install the previous version again and restore it. |
| An Ubuntu import must be undone | Restore the `pre-restore-…` backup named in the migration report |

## 5. If the PC is lost while positions are open

Positions keep their broker-side stops. From any computer, use the broker's own MT5 terminal or app to manage them.
When RR Trading One runs again, it adopts the broker's positions and protects any that lack a stop before allowing a
new entry.
