#!/usr/bin/env bash
# Restore a pg_dump archive. Stops trading services first; trading resumes only after the worker's
# startup reconciliation passes against the live MT5 terminal (positions are never re-opened from a backup).
# Usage: scripts/restore.sh backups/rr_YYYYmmddTHHMMSSZ.dump
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?usage: restore.sh <dump file>}"
[[ -f "$FILE" ]] || { echo "no such file $FILE"; exit 1; }
if [[ -f "$FILE.sha256" ]]; then sha256sum -c "$FILE.sha256"; fi
read -r -p "This REPLACES the current database with $FILE. Type RESTORE to continue: " ok
[[ "$ok" == "RESTORE" ]] || { echo "aborted"; exit 1; }
set -a; . ./.env; set +a
echo "==> Safety backup of current state"
./scripts/backup.sh 30
docker compose stop worker api
docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction < "$FILE"
# Force reconciliation and disable automation after any restore
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "UPDATE trading_state SET auto_trading=false, startup_checks_passed=false;"
docker compose run --rm migrate
docker compose up -d api worker
echo "restore complete — auto trading is OFF; verify reconciliation in System Monitoring before re-enabling."
