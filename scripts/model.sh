#!/usr/bin/env bash
# Switch the model the agent uses. Edits only the local .env (git-ignored); keys are typed hidden and never printed.
#
#   scripts/model.sh                         show the current model settings (no keys)
#   scripts/model.sh openai   <model>        OpenAI          (key: OPENAI_API_KEY)
#   scripts/model.sh moonshot <model>        Kimi            (key: MOONSHOT_API_KEY)
#   scripts/model.sh zai      <model>        GLM             (key: ZAI_API_KEY)
#   scripts/model.sh ollama   <model>        local Ollama    (no key; run `ollama serve` first)
#   scripts/model.sh openai_compatible <model> <base_url>    any other OpenAI-compatible server
#
# Each provider keeps its own key variable, so switching back and forth does not ask for keys again.
# Restart the server (scripts/start.sh) after switching: settings are read at startup.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"; PY="${PY:-python3}"
[[ -f .env ]] || { echo "No .env yet: run scripts/setup.sh first." >&2; exit 2; }

get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
set_kv() { "$PY" - "$1" "$2" <<'PYEOF'
import re, sys
key, value = sys.argv[1], sys.argv[2]
lines = open(".env", encoding="utf-8").read().splitlines()
lines = [l for l in lines if not re.match(rf"\s*#?\s*{re.escape(key)}=", l)]
lines.append(f"{key}={value}")
open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
PYEOF
}
unset_kv() { "$PY" - "$1" <<'PYEOF'
import re, sys
key = sys.argv[1]
lines = open(".env", encoding="utf-8").read().splitlines()
open(".env", "w", encoding="utf-8").write("\n".join(l for l in lines if not re.match(rf"\s*{re.escape(key)}=", l)) + "\n")
PYEOF
}

if [[ $# -eq 0 ]]; then
  echo "provider: $(get FORGE_MODEL_PROVIDER || true)"; echo "model:    $(get FORGE_MODEL_NAME || true)"
  echo "base_url: $(get FORGE_MODEL_BASE_URL || true)"
  for k in OPENAI_API_KEY MOONSHOT_API_KEY ZAI_API_KEY FORGE_MODEL_API_KEY; do
    v="$(get "$k")"; [[ -n "$v" && "$v" != YOUR_* ]] && echo "$k: set" || echo "$k: -"
  done
  exit 0
fi

provider="$1"; model="${2:-}"
case "$provider" in
  openai) key_var=OPENAI_API_KEY ;;
  moonshot) key_var=MOONSHOT_API_KEY ;;
  zai) key_var=ZAI_API_KEY ;;
  ollama|openai_compatible) key_var="" ;;
  *) echo "Unknown provider '$provider'. Use: openai | moonshot | zai | ollama | openai_compatible" >&2; exit 2 ;;
esac
[[ -n "$model" ]] || { echo "Model name is required, e.g.: scripts/model.sh $provider <model-id>" >&2; exit 2; }

set_kv FORGE_MODEL_PROVIDER "$provider"
set_kv FORGE_MODEL_NAME "$model"
if [[ "$provider" == openai_compatible ]]; then
  [[ -n "${3:-}" ]] || { echo "openai_compatible needs a base URL: scripts/model.sh openai_compatible <model> <url>" >&2; exit 2; }
  set_kv FORGE_MODEL_BASE_URL "$3"
else
  unset_kv FORGE_MODEL_BASE_URL          # each provider has a default; a stale URL from another provider would win
fi
# A price copied for the previous model is wrong for this one: drop it rather than report a wrong cost.
for k in FORGE_PRICE_INPUT_USD_PER_MILLION FORGE_PRICE_OUTPUT_USD_PER_MILLION FORGE_PRICE_CACHED_INPUT_USD_PER_MILLION; do
  unset_kv "$k"
done
echo "✓ model: $provider / $model (prices cleared: set FORGE_PRICE_* for this model, or cost shows 'not priced')"

if [[ -n "$key_var" ]]; then
  cur="$(get "$key_var")"
  if [[ -z "$cur" || "$cur" == YOUR_* ]]; then
    read -r -s -p "Paste $key_var (input is hidden), Enter to skip: " key; echo
    if [[ -n "$key" ]]; then set_kv "$key_var" "$key"; echo "✓ $key_var saved to .env"; else echo "! skipped: runs will answer 503 until it is set"; fi
  else
    echo "✓ $key_var already set"
  fi
fi
chmod 600 .env
echo "Restart the server (Ctrl+C in its terminal, then scripts/start.sh) to use it."
