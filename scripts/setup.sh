#!/usr/bin/env bash
# One-time interactive setup of the local .env. Run it once; re-running is safe.
#
# What it does for you (so you never edit .env by hand):
#   - creates .env from .env.example if it does not exist (never overwrites an existing one)
#   - generates a random PostgreSQL password if .env still has the placeholder
#   - asks for OPENAI_API_KEY (typed hidden, never echoed, never printed) and FORGE_MODEL_NAME
#   - protects the file (chmod 600)
# The key goes ONLY into your local .env, which git ignores.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"; PY="${PY:-python3}"
[[ -f .env ]] || { cp .env.example .env; echo "✓ created .env"; }

get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

# Safe in-place update: python rewrites the file, so special characters in values can never break a shell/sed.
set_kv() { "$PY" - "$1" "$2" <<'PYEOF'
import re, sys
key, value = sys.argv[1], sys.argv[2]
lines = open(".env", encoding="utf-8").read().splitlines()
lines = [l for l in lines if not re.match(rf"\s*#?\s*{re.escape(key)}=", l)]   # drop old/commented versions
lines.append(f"{key}={value}")
open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
PYEOF
}

# 1. PostgreSQL password
pw="$(get POSTGRES_PASSWORD)"
if [[ -z "$pw" || "$pw" == CHANGE_ME* ]]; then
  # The DB volume remembers the password of its FIRST start. If a volume already exists, a new password would
  # not match it, so offer to recreate it (it only contains local ForgeMini data).
  # Ask compose for its project name (it derives it from the directory name) and look for that project's volume.
  project="$(docker compose config --format json 2>/dev/null | "$PY" -c 'import sys, json; print(json.load(sys.stdin)["name"])' 2>/dev/null || true)"
  if [[ -n "$project" ]] && [[ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.volume=postgres_data" 2>/dev/null)" ]]; then
    echo "! A local database from an earlier start exists; it was created with a different password."
    read -r -p "  Reset it? This DELETES the local ForgeMini database (safe at this stage). [y/N] " ans
    if [[ "$ans" =~ ^[yY]$ ]]; then docker compose down -v >/dev/null 2>&1 && echo "✓ old database removed"
    else echo "  Keeping it: then set POSTGRES_PASSWORD in .env to the OLD password yourself and re-run." >&2; exit 1; fi
  fi
  set_kv POSTGRES_PASSWORD "$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
  echo "✓ generated a random PostgreSQL password"
else
  echo "✓ PostgreSQL password already set"
fi

# 2. OpenAI key (hidden input)
key="$(get OPENAI_API_KEY)"
if [[ -z "$key" || "$key" == YOUR_* ]]; then
  read -r -s -p "Paste your OPENAI_API_KEY (input is hidden), Enter to skip: " key; echo
  if [[ -n "$key" ]]; then set_kv OPENAI_API_KEY "$key"; echo "✓ key saved to .env"; else echo "! skipped: runs will answer 503 until it is set"; fi
else
  echo "✓ OPENAI_API_KEY already set"
fi

# 3. Model name
model="$(get FORGE_MODEL_NAME)"
if [[ -z "$model" || "$model" == YOUR_* ]]; then
  read -r -p "Model name to use (the OpenAI model id you chose), Enter to skip: " model
  if [[ -n "$model" ]]; then set_kv FORGE_MODEL_NAME "$model"; echo "✓ model saved"; else echo "! skipped: runs will answer 503 until it is set"; fi
else
  echo "✓ FORGE_MODEL_NAME already set"
fi

chmod 600 .env
echo
echo "Done. Next: scripts/start.sh"
