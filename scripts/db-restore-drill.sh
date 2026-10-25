#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# db-restore-drill.sh -- automated local restore drill (Stage 8.1).
#
# Proves the dump -> restore roundtrip end to end on real Docker
# PostgreSQL 17, without touching the local compose data volume:
#
#   1. start a throwaway `drill` PostgreSQL container on the compose network
#   2. apply all Django migrations into it and seed one fixture superuser
#   3. pg_dump it (same code path as backup-db.sh output)
#   4. restore the dump into a second empty database
#   5. verify: identical table lists, identical per-table row counts,
#      zero unapplied migrations and the seeded user readable via Django
#   6. clean the drill container up (the drill dump stays in backups/)
#
# Usage:  scripts/db-restore-drill.sh
#
# Run this before first deploy and after major migrations; record the
# result in docs/deployment.md section 7 (restore drill log).
# ---------------------------------------------------------------------------

DRILL_CONTAINER="earnings-radar-restore-drill"
DRILL_DUMP_USER="postgres"
DRILL_SOURCE_DB="drill_source"
DRILL_RESTORED_DB="drill_restored"
DRILL_SUPERUSER_EMAIL="drill-admin@example.com"
DRILL_SUPERUSER_PASSWORD="drill-password-only-fixture"
WEB_ENV=(
    -e POSTGRES_HOST="$DRILL_CONTAINER"
    -e POSTGRES_PORT=5432
    -e POSTGRES_USER="$DRILL_DUMP_USER"
    -e POSTGRES_PASSWORD="drill-password"
    -e POSTGRES_DB="$DRILL_SOURCE_DB"
)

step() { printf '\n== %s ==\n' "$1"; }
pass() { printf 'PASS: %s\n' "$1"; }

cd "$(git rev-parse --show-toplevel)"
mkdir -p backups
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="backups/restore-drill-${TIMESTAMP}.sql"

step "0. compose network"
docker compose up -d db >/dev/null
DB_CONTAINER_ID="$(docker compose ps -q db)"
NETWORK="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$DB_CONTAINER_ID")"
echo "compose network: ${NETWORK}"

docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true

step "1. throwaway drill postgres (no host ports published)"
docker run -d --name "$DRILL_CONTAINER" --network "$NETWORK" \
    -e POSTGRES_USER="$DRILL_DUMP_USER" \
    -e POSTGRES_PASSWORD="drill-password" \
    -e POSTGRES_DB="$DRILL_SOURCE_DB" \
    postgres:17-alpine >/dev/null
for _ in $(seq 1 60); do
    if docker exec "$DRILL_CONTAINER" pg_isready -U "$DRILL_DUMP_USER" -d "$DRILL_SOURCE_DB" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$DRILL_CONTAINER" pg_isready -U "$DRILL_DUMP_USER" -d "$DRILL_SOURCE_DB" >/dev/null
pass "drill postgres ready"

step "2. migrate + seed into ${DRILL_SOURCE_DB}"
docker compose run --rm "${WEB_ENV[@]}" web python manage.py migrate --noinput >/dev/null
docker compose run --rm "${WEB_ENV[@]}" \
    -e DJANGO_SUPERUSER_EMAIL="$DRILL_SUPERUSER_EMAIL" \
    -e DJANGO_SUPERUSER_PASSWORD="$DRILL_SUPERUSER_PASSWORD" \
    web python manage.py createsuperuser --noinput >/dev/null
pass "migrations applied and fixture superuser created"

step "3. dump ${DRILL_SOURCE_DB}"
docker exec "$DRILL_CONTAINER" \
    pg_dump -U "$DRILL_DUMP_USER" "$DRILL_SOURCE_DB" > "$DUMP_FILE"
DUMP_SIZE="$(wc -c < "$DUMP_FILE" | tr -d ' ')"
if [[ "$DUMP_SIZE" -lt 10000 ]]; then
    echo "ERROR: drill dump suspiciously small (${DUMP_SIZE} bytes)" >&2
    exit 1
fi
pass "dump written: ${DUMP_FILE} (${DUMP_SIZE} bytes)"

step "4. restore into empty ${DRILL_RESTORED_DB}"
docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" \
    -c "CREATE DATABASE ${DRILL_RESTORED_DB}" >/dev/null
docker exec -i "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" \
    -d "$DRILL_RESTORED_DB" -v ON_ERROR_STOP=1 -q < "$DUMP_FILE"
pass "restore completed without SQL errors"

step "5a. table lists identical"
docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" -d "$DRILL_SOURCE_DB" -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename" \
    > backups/.drill-tables-source.txt
docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" -d "$DRILL_RESTORED_DB" -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename" \
    > backups/.drill-tables-restored.txt
if ! diff -u backups/.drill-tables-source.txt backups/.drill-tables-restored.txt; then
    echo "ERROR: table lists differ between source and restored database" >&2
    exit 1
fi
TABLE_COUNT="$(wc -l < backups/.drill-tables-source.txt | tr -d ' ')"
rm -f backups/.drill-tables-source.txt backups/.drill-tables-restored.txt
pass "${TABLE_COUNT} tables, identical lists"

step "5b. per-table row counts identical"
MISMATCH=0
while IFS= read -r table; do
    SOURCE_COUNT="$(docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" -d "$DRILL_SOURCE_DB" -tAc \
        "SELECT count(*) FROM \"${table}\"")"
    RESTORED_COUNT="$(docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" -d "$DRILL_RESTORED_DB" -tAc \
        "SELECT count(*) FROM \"${table}\"")"
    if [[ "$SOURCE_COUNT" != "$RESTORED_COUNT" ]]; then
        echo "MISMATCH: ${table} source=${SOURCE_COUNT} restored=${RESTORED_COUNT}" >&2
        MISMATCH=1
    fi
done < <(docker exec "$DRILL_CONTAINER" psql -U "$DRILL_DUMP_USER" -d "$DRILL_SOURCE_DB" -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
if [[ "$MISMATCH" -ne 0 ]]; then
    exit 1
fi
pass "row counts match for all ${TABLE_COUNT} tables"

step "5c. Django reads the restored database"
docker compose run --rm "${WEB_ENV[@]}" -e POSTGRES_DB="$DRILL_RESTORED_DB" web \
    python manage.py shell -c '
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

executor = MigrationExecutor(connection)
pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
assert not pending, f"unapplied migrations: {len(pending)}"
users = get_user_model().objects.count()
assert users == 1, f"expected exactly 1 user, found {users}"
print("DRILL_DJANGO_RESTORED_OK users=1")
' | grep -q "DRILL_DJANGO_RESTORED_OK"
pass "zero unapplied migrations and seeded user readable via Django ORM"

step "6. cleanup"
docker rm -f "$DRILL_CONTAINER" >/dev/null
pass "drill container removed (drill dump kept: ${DUMP_FILE})"

printf '\nRESTORE DRILL PASSED (%s)\n' "$TIMESTAMP"
