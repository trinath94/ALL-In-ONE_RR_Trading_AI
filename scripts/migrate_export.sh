#!/usr/bin/env bash
# Export the Ubuntu deployment's database for RR Trading One on Windows (Phase 23, specification Part 23).
#
# Read-only: a logical PostgreSQL dump and a manifest. The server keeps running and is not changed in any way, so it
# stays available for rollback until the Windows application is accepted. (Never copy a Docker volume instead: a volume
# is tied to this PostgreSQL build and operating system; a logical dump is portable and can be verified.)
#
# The row counts and the dump are taken from ONE exported snapshot (a read-only REPEATABLE READ transaction held open
# while pg_dump runs with --snapshot), so they describe exactly the same data even while the server is working.
#
# Writes into backups/ (same protection as scripts/backup.sh):
#   rr_migrate_<stamp>.dump            pg_dump custom format
#   rr_migrate_<stamp>.manifest.json   sha256 and size of the dump, schema (Alembic) version, PostgreSQL version and
#                                      the row count of every table
# Copy BOTH files to the PC (same folder), then in RR Trading One: Backup and Recovery -> Import from the previous
# Ubuntu server -> choose the .dump. The PC checks the dump against the manifest before anything else.
#
# Usage: scripts/migrate_export.sh                          (in the deployment folder: its .env and docker compose)
#        RR_PG_CONTAINER=<name> scripts/migrate_export.sh   (a PostgreSQL container addressed directly)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
: "${POSTGRES_USER:?POSTGRES_USER not set (.env)}" "${POSTGRES_DB:?POSTGRES_DB not set (.env)}"
if [ -n "${RR_PG_CONTAINER:-}" ]; then pgx() { docker exec -i "$RR_PG_CONTAINER" "$@"; }
else pgx() { docker compose exec -T postgres "$@"; }; fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUTDIR=${RR_EXPORT_DIR:-backups}
mkdir -p "$OUTDIR"
DUMP="$OUTDIR/rr_migrate_${STAMP}.dump"
MANIFEST="$OUTDIR/rr_migrate_${STAMP}.manifest.json"
COUNTS=$(mktemp)
trap 'rm -f "$COUNTS"' EXIT

# 1. Open a read-only snapshot and export it.
coproc PG { pgx psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -q -v ON_ERROR_STOP=1; }
echo "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" >&"${PG[1]}"
echo "SELECT pg_export_snapshot();" >&"${PG[1]}"
read -r -t 60 SNAPSHOT <&"${PG[0]}" || { echo "could not open a snapshot" >&2; exit 1; }

# 2. Row counts, schema and server version, in that snapshot.
cat >&"${PG[1]}" <<'SQL'
SELECT 'table|' || table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', table_name), false, true, '')))[1]::text
  FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1;
SELECT 'alembic|' || version_num FROM alembic_version;
SELECT 'server|' || current_setting('server_version');
SELECT 'END';
SQL
while read -r -t 600 line <&"${PG[0]}"; do [ "$line" = END ] && break; echo "$line" >> "$COUNTS"; done

# 3. The dump, in the same snapshot; then a readability check of the archive.
pgx pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --snapshot="$SNAPSHOT" --format=custom --no-owner > "$DUMP"
echo "COMMIT;" >&"${PG[1]}"
exec {PG[1]}>&-
wait "$PG_PID" 2>/dev/null || true
pgx pg_restore --list < "$DUMP" > /dev/null

# 4. Manifest.
python3 - "$DUMP" "$COUNTS" "$MANIFEST" "$POSTGRES_DB" <<'PY'
import datetime, hashlib, json, os, sys
dump, counts, manifest, db = sys.argv[1:5]
h = hashlib.sha256()
with open(dump, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
tables, alembic, server = {}, None, None
for line in open(counts):
    kind, _, rest = line.strip().partition("|")
    if kind == "table":
        name, _, n = rest.rpartition("|")
        tables[name] = int(n)
    elif kind == "alembic":
        alembic = rest
    elif kind == "server":
        server = rest
if not tables or not alembic:
    sys.exit("the database did not report its tables or schema version; nothing exported")
json.dump({"format": "rr-migrate-manifest/1", "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "dump": os.path.basename(dump), "sha256": h.hexdigest(), "bytes": os.path.getsize(dump), "database": db,
           "alembic_version": alembic, "postgres_version": server, "tables": tables},
          open(manifest, "w"), indent=1, sort_keys=True)
print(f"export ok: {dump} ({os.path.getsize(dump)} bytes, sha256 {h.hexdigest()[:16]}...), schema {alembic}, "
      f"{len(tables)} tables, {sum(tables.values())} rows")
PY
chmod 600 "$DUMP" "$MANIFEST"
echo "manifest: $MANIFEST"
echo "Nothing on this server was changed. Before switching to the PC, switch auto trading OFF here. Keep this server"
echo "until the Windows application is accepted: it is your rollback."
