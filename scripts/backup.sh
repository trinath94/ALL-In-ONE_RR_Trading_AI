#!/usr/bin/env bash
# Consistent PostgreSQL backup (pg_dump custom format) + config, with retention.
# Usage: scripts/backup.sh [retention_days=14]
set -euo pipefail
cd "$(dirname "$0")/.."
RETENTION="${1:-14}"
set -a; . ./.env; set +a
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p backups
OUT="backups/rr_${STAMP}.dump"
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner > "$OUT"
# Integrity check: the archive must list its table of contents
docker compose exec -T postgres pg_restore --list < "$OUT" > /dev/null
sha256sum "$OUT" > "$OUT.sha256"
# Config backup (contains secrets: keep with the same protection as the DB dump)
tar -czf "backups/rr_config_${STAMP}.tgz" .env nginx/nginx.conf secrets 2>/dev/null || true
chmod 600 backups/rr_*"${STAMP}"*
find backups -name 'rr_*' -type f -mtime "+${RETENTION}" -delete
echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
