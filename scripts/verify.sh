#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# verify.sh -- run the standard quality gates (AGENTS.md section 11) in ONE
# container, fail-fast, cheapest first:
#   ruff check -> ruff format --check -> Django check
#   -> makemigrations --check -> mypy -> pytest
#
# Usage:
#   ./scripts/verify.sh                full gate
#   ./scripts/verify.sh -k watchlists  extra args pass through to pytest
#   ./scripts/verify.sh --help
#
# Equivalent to running the six `docker compose run --rm web ...` commands
# listed in AGENTS.md section 11, without paying container startup six times.
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    grep '^#' "$0" | cut -c 3- | sed -n '2,20p'
    exit 0
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PYTEST_ARGS="${*:-}"

exec docker compose run --rm web sh -ec "
    ruff check . &&
    ruff format --check . &&
    python manage.py check &&
    python manage.py makemigrations --check --dry-run &&
    mypy . &&
    pytest ${PYTEST_ARGS}
"
