#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# restore-db.sh -- restore a plain SQL dump produced by backup-db.sh.
#
# Usage:
#   scripts/restore-db.sh --file backups/foo.sql            # local compose db
#   RESTORE_DATABASE_URL=postgresql://... \
#     scripts/restore-db.sh --file backups/foo.sql --url    # any URL target
#
# This script DESTROYS the target database schema (DROP SCHEMA public
# CASCADE) before restoring.  It therefore refuses to run without
# --i-know-this-wipes-the-target.  Prefer the platform's point-in-time
# restore for disaster recovery (docs/deployment.md section 7); use this
# for the scheduled restore drills and controlled rebuilds.
# ---------------------------------------------------------------------------

FILE=""
MODE="compose"
ACK="no"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) FILE="$2"; shift 2 ;;
        --url) MODE="url"; shift ;;
        --i-know-this-wipes-the-target) ACK="yes"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$FILE" || ! -f "$FILE" ]]; then
    echo "--file <existing dump> is required." >&2
    exit 2
fi
if [[ "$ACK" != "yes" ]]; then
    echo "Refusing to run: restoring wipes the target schema first." >&2
    echo "Re-run with --i-know-this-wipes-the-target if that is intended." >&2
    exit 2
fi

if [[ "$MODE" == "compose" ]]; then
    POSTGRES_USER="${POSTGRES_USER:-earnings_radar}"
    POSTGRES_DB="${POSTGRES_DB:-earnings_radar}"
    echo "Resetting schema of compose service 'db' (database: ${POSTGRES_DB})..."
    docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -v ON_ERROR_STOP=1 \
        -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    echo "Restoring ${FILE}..."
    docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -v ON_ERROR_STOP=1 -q < "$FILE"
    echo "OK: restored ${FILE} into compose db ${POSTGRES_DB}."
else
    if [[ -z "${RESTORE_DATABASE_URL:-}" ]]; then
        echo "RESTORE_DATABASE_URL is required with --url." >&2
        exit 2
    fi
    echo "Resetting schema at RESTORE_DATABASE_URL (throwaway postgres:17-alpine container)..."
    docker run --rm -i -e RESTORE_DATABASE_URL postgres:17-alpine \
        sh -c 'psql --dbname="$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 \
               -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"' \
        < /dev/null
    echo "Restoring ${FILE}..."
    docker run --rm -i -e RESTORE_DATABASE_URL postgres:17-alpine \
        sh -c 'psql --dbname="$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -q' \
        < "$FILE"
    echo "OK: restored ${FILE} into RESTORE_DATABASE_URL target."
fi
