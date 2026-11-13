# Recovery guide

**Golden rule:** the broker is the source of truth. Protective SL/TP orders live at the broker, so
stopping the Linux stack does **not** close or unprotect positions. It also does not manage them:
break-even and trailing pause. Use the kill switch policy if positions must be reduced or closed.

| Situation | Action |
|---|---|
| Linux server restart | `docker compose up -d`. The worker re-syncs from MT5, reconciles the execution journal, checks that every position has a stop (applying PROTECT if not), and only then sets `startup_checks_passed`. Positions are never re-opened from old application state. |
| Windows/connector restart | The service auto-starts. Its watchdog re-initialises MT5 and reconciles SENDING/UNKNOWN journal entries. The Linux worker sees the heartbeat return and re-runs startup checks. |
| Network partition while an order was in flight | The order shows `UNKNOWN` in Active Trades → Recent orders. It resolves automatically through journal lookup, or manually with `POST /api/orders/reconcile`. Never re-enter it by hand while it is UNKNOWN. |
| Risk limit tripped | Trading locks (`risk_lock`), and the configured position policy is applied. An administrator reviews it, then resets it from the Control Center. Reset forces a fresh reconciliation. |
| Database loss | `scripts/restore.sh backups/rr_<stamp>.dump`. It takes a safety backup first, restores in one transaction, forces auto trading OFF, and requires reconciliation. |
| Lost `RR_ENCRYPTION_KEY` | Stored broker passwords cannot be decrypted. Re-enter them in Settings. Trading itself does not depend on them, because the connector holds its own credentials. |
| Compromised secret | Rotate `RR_BRIDGE_SHARED_SECRET` on both sides, rotate `RR_JWT_SECRET` (all sessions end), restart. |

Backups: `scripts/backup.sh` runs daily from cron. It produces a custom-format dump, verifies it with
`pg_restore --list`, writes a SHA-256 checksum, saves a config tarball, and keeps 14 days. Test a
restore periodically on a staging host.
