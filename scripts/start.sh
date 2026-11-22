#!/usr/bin/env bash
# One-command local start of the ForgeMini backend with preflight checks.
#
# Why a script: the manual sequence fails in confusing ways (a global `uvicorn` shadowing the
# venv one, missing dependencies, placeholder passwords). Each check below names ONE concrete
# problem and its fix. The script never prints secret values.
#
# Usage (from anywhere, with your venv active):  scripts/start.sh
# Env: SKIP_COMPOSE=1 to not touch docker compose (PostgreSQL already running elsewhere).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { echo "✗ $1" >&2; shift; for l in "$@"; do echo "    $l" >&2; done; exit 1; }
ok()   { echo "✓ $1"; }

# 1. A virtualenv must be active and we use ITS interpreter, never whatever `uvicorn` is on PATH.
[[ -n "${VIRTUAL_ENV:-}" ]] || fail "No virtualenv is active." \
  "python3 -m venv .venv && source .venv/bin/activate"
PY="${VIRTUAL_ENV}/bin/python"
[[ -x "$PY" ]] || fail "Cannot find $PY."
ok "interpreter: $PY"

# 2. Dependencies are installed into THAT interpreter.
if ! "$PY" -c "import fastapi, uvicorn, sqlalchemy, asyncpg, alembic, langchain_core, langchain_openai" 2>/dev/null; then
  fail "Backend dependencies are not installed in this virtualenv." \
    'pip install -e "backend[dev]" -e "cli[dev]"'
fi
(cd backend && "$PY" -c "import app.bootstrap") >/dev/null 2>&1 \
  || fail "The backend package does not import (see: cd backend && $PY -c 'import app.bootstrap')."
ok "backend dependencies"

# 3. .env: exists, no placeholder password, model settings present (values are never printed).
[[ -f .env ]] || fail ".env is missing." "Run the one-time setup:  scripts/setup.sh"
# Read single keys textually instead of `source .env`: sourcing would EXECUTE the file, and a password
# containing $ # or spaces would break it (or run something unintended).
# Process environment wins over .env (same precedence as the backend's pydantic Settings), and we only
# READ into local variables: assigning to e.g. POSTGRES_HOST would overwrite an exported value.
dotenv() { local v; v="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; printf '%s' "$v"; }
cfg()    { local cur="${!1:-}"; if [[ -n "$cur" ]]; then printf '%s' "$cur"; else dotenv "$1"; fi; }
PG_PASSWORD="$(cfg POSTGRES_PASSWORD)"
PG_HOST="$(cfg POSTGRES_HOST)"; PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="$(cfg POSTGRES_PORT)"; PG_PORT="${PG_PORT:-55432}"
API_KEY="$(cfg OPENAI_API_KEY)"; MODEL_NAME="$(cfg FORGE_MODEL_NAME)"
IMAGE="$(cfg FORGE_SANDBOX_IMAGE)"; IMAGE="${IMAGE:-python:3.12-slim}"
[[ -n "$PG_PASSWORD" && "$PG_PASSWORD" != CHANGE_ME* ]] || fail \
  "POSTGRES_PASSWORD in .env is empty or still the placeholder." \
  "Run the one-time setup, it generates a password for you:  scripts/setup.sh"
ok ".env: postgres settings"
if [[ -z "$API_KEY" || "$API_KEY" == YOUR_* || -z "$MODEL_NAME" || "$MODEL_NAME" == YOUR_* ]]; then
  echo "! OPENAI_API_KEY / FORGE_MODEL_NAME are not set in .env: the server will start, but starting a run will answer 503." >&2
else
  ok ".env: model settings present"
fi

# 4. Docker daemon (the sandbox needs it) and PostgreSQL.
docker info >/dev/null 2>&1 || fail "Docker is not running or not reachable." "Start Docker Desktop and retry."
ok "docker daemon"
if [[ "${SKIP_COMPOSE:-0}" != "1" ]]; then
  # Keep compose's output: on a first start the usual failure is that the image cannot be DOWNLOADED
  # (no internet, Docker Hub blocked or rate-limited). Then no container exists and `compose logs` is empty,
  # so "did not become healthy" would send the user to the wrong place.
  if ! out="$(docker compose up -d --wait postgres 2>&1)"; then
    echo "$out" | tail -5 >&2
    if grep -qiE "pull|resolve reference|registry|manifest|toomanyrequests" <<<"$out"; then
      fail "Cannot download the PostgreSQL image (see the error above)." \
        "Check the internet connection and access to Docker Hub, then retry."
    fi
    fail "PostgreSQL did not become healthy." "docker compose logs postgres | tail -20"
  fi
fi
"$PY" - "$PG_HOST" "$PG_PORT" <<'PYEOF' || fail "Cannot reach PostgreSQL on $PG_HOST:$PG_PORT." "docker compose ps; check POSTGRES_PORT in .env"
import socket, sys
socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
PYEOF
ok "postgres reachable on $PG_HOST:$PG_PORT"

# 5. Migrations (run from backend/ so alembic.ini is found); auth errors mean a password mismatch.
if ! out="$(cd backend && "$PY" -m alembic upgrade head 2>&1)"; then
  echo "$out" | tail -12 >&2
  if grep -qi "password authentication failed" <<<"$out"; then
    fail "Migrations failed: wrong database password." \
      ".env password differs from the one the DB volume was created with" \
      "(fix .env, or recreate the volume: docker compose down -v  — this DELETES the local database)."
  fi
  fail "Migrations failed (see the error above)."
fi
ok "database migrated"

# 6. Sandbox image: pulling it now avoids a long silent wait inside the first run.
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "… pulling sandbox image $IMAGE"; docker pull -q "$IMAGE" >/dev/null || fail "Cannot pull $IMAGE."; }
ok "sandbox image $IMAGE"

echo
echo "Server starting on http://127.0.0.1:${FORGE_PORT:-8000}. Leave this terminal OPEN (Ctrl+C stops the server)."
echo "Use ANOTHER terminal for commands:"
echo "    cd $ROOT && source .venv/bin/activate"
echo "    forge solve --repo examples/demo-calc --goal \"average() returns wrong values for integer lists\" --test \"python -m unittest tests.test_average\""
echo
cd backend
exec "$PY" -m uvicorn app.bootstrap:create_forge_app --factory --host 127.0.0.1 --port "${FORGE_PORT:-8000}"
