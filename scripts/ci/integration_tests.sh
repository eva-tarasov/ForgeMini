#!/usr/bin/env bash
# Integration tests against a real PostgreSQL.
#
# Why a script: the old test-suite guards every integration test with its own
# hard-coded database-name pattern (forge_task03_*, _04_, _05_, _06_, week4_audit_*)
# and a single POSTGRES_DB, so one pytest run cannot cover all of them.
# This script creates one throw-away DB per pattern, migrates it with Alembic and
# runs the matching test files. It never touches a database that it did not create.
#
# Required env: POSTGRES_USER, POSTGRES_PASSWORD
# Optional env: POSTGRES_HOST (127.0.0.1), POSTGRES_PORT (5432), PYTHON (python)
set -euo pipefail

: "${POSTGRES_USER:?set POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
export POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
PYTHON="${PYTHON:-python}"
export PGPASSWORD="$POSTGRES_PASSWORD"

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../backend" && pwd)"
cd "$BACKEND_DIR"

psql_admin() {
  psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

fresh_db() {  # drop + create + migrate one database
  local name="$1"
  psql_admin -q -c "DROP DATABASE IF EXISTS \"$name\" WITH (FORCE)"
  psql_admin -q -c "CREATE DATABASE \"$name\""
  POSTGRES_DB="$name" "$PYTHON" -m alembic upgrade head >/dev/null
}

run_group() {  # run_group <db-name> <extra env as KEY=VAL ...> -- <pytest args>
  local db="$1"; shift
  local -a envs=()
  while [[ "$1" != "--" ]]; do envs+=("$1"); shift; done
  shift
  echo "=== $db :: $* ==="
  fresh_db "$db"
  env POSTGRES_DB="$db" "${envs[@]}" "$PYTHON" -m pytest -q -p no:cacheprovider "$@"
}

T06=forge_task06_week4_audit_ci   # also satisfies the forge_task06_* and week4_audit_* patterns

run_group forge_task03_ci FORGE_TEST_SCHEMA_DB=forge_task03_ci FORGE_TEST_POSTGRES=1 -- \
  tests/test_schema_integration.py tests/test_postgres_integration.py

run_group forge_task04_ci FORGE_TASK04_TEST_DB=forge_task04_ci -- \
  tests/test_repository_integration.py

run_group forge_task05_ci FORGE_TASK05_TEST_DB=forge_task05_ci -- \
  tests/test_execution_schema_integration.py

# Everything else that needs the "task06 / week4_audit" fixture DB (the kernel's run lifecycle, reads and
# persistence tests plus the ForgeMini sandbox/E2E tests).
run_group "$T06" FORGE_TASK06_TEST_DB="$T06" -- \
  $(cd tests && ls *_integration.py | grep -vE '^(test_schema_integration|test_postgres_integration|test_repository_integration|test_execution_schema_integration)\.py$' | sed 's|^|tests/|') \
  tests/test_service_transactions.py \
  tests/test_forgemini_http_e2e.py \
  tests/test_demo_shop_e2e.py \
  tests/test_cli_against_real_server.py \
  tests/test_evals_e2e.py   # these names do not end in _integration.py, so they are listed explicitly
