#!/usr/bin/env bash
# Mini-evals runner.   scripts/eval.sh list | run [--runs N] [--tasks a,b] [--model NAME] [--yes] | report FILE.json | compare A.json B.json...
# Needs the server running (scripts/start.sh in another terminal) and the venv with forge-cli installed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! python3 -c "import forge_cli" >/dev/null 2>&1; then
  echo "forge-cli is not importable: activate the venv (source .venv/bin/activate) and pip install -e cli" >&2; exit 3
fi
PYTHONPATH="$ROOT/evals${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m evalkit "$@"
