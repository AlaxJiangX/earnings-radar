#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# backup-db.sh -- dump the database to backups/<name>.sql (Stage 8.1).
#
# Usage:
#   scripts/backup-db.sh                       # local: dump the docker
#                                              # compose `db` service
#   BACKUP_DATABASE_URL=postgresql://... \
#     scripts/backup-db.sh --url               # platform: dump any
#                                              # reachable PostgreSQL URL
#   scripts/backup-db.sh --output-dir DIR      # override target directory
#
# The dump is plain SQL (psql-restorable, grep-able, small for alpha
# scale).  Files are written to backups/ which is gitignored.  Keep at
# least the ADR-028 backup policy in mind: daily backups, 30-day
# retention -- on Railway the volume backups (daily/weekly/monthly)
# cover this; this script is the manual/logical fallback.
#
# Version note: run pg_dump with the same major version as the server
# (the compose `db` service and the platform default are PostgreSQL 17).
# ---------------------------------------------------------------------------

OUTPUT_DIR="backups"
MODE="compose"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) MODE="url"; shift ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$OUTPUT_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_FILE="${OUTPUT_DIR}/earnings-radar-${TIMESTAMP}.sql"

if [[ "$MODE" == "compose" ]]; then
    POSTGRES_USER="${POSTGRES_USER:-earnings_radar}"
    POSTGRES_DB="${POSTGRES_DB:-earnings_radar}"
    echo "Dumping compose service 'db' (database: ${POSTGRES_DB})..."
    docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
        > "$OUTPUT_FILE"
else
    if [[ -z "${BACKUP_DATABASE_URL:-}" ]]; then
        echo "BACKUP_DATABASE_URL is required with --url." >&2
        exit 2
    fi
    echo "Dumping BACKUP_DATABASE_URL via a throwaway postgres:17-alpine container..."
    # The URL is passed through the environment, not the command line, so
    # it never shows up in `docker ps` or shell history listings.
    docker run --rm -e BACKUP_DATABASE_URL postgres:17-alpine \
        sh -c 'pg_dump --dbname="$BACKUP_DATABASE_URL" -n public --no-owner' \
        > "$OUTPUT_FILE"
fi

SIZE="$(wc -c < "$OUTPUT_FILE" | tr -d ' ')"
if [[ "$SIZE" -lt 1000 ]]; then
    echo "ERROR: dump looks suspiciously small (${SIZE} bytes); not trusting it." >&2
    exit 1
fi

echo "OK: ${OUTPUT_FILE} (${SIZE} bytes)"
echo "Restore with: scripts/restore-db.sh --file ${OUTPUT_FILE}  (or psql < ${OUTPUT_FILE})"
