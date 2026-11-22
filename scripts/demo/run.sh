#!/usr/bin/env bash
# ForgeMini live demo: somebody else's repo, a real bug, one command, a verified fix.
#
#   scripts/demo/run.sh                 # issue #1, press Enter between scenes (live presenting)
#   scripts/demo/run.sh --issue 2       # issue 1, 2 or 3  (each is a separate bug, ~1-2 minutes)
#   scripts/demo/run.sh --all           # all three bugs in one run (whole test suite)
#   scripts/demo/run.sh --auto          # timed pauses instead of Enter (for screen recording / asciinema)
#   scripts/demo/run.sh --fast          # no pauses at all (used by the tests)
#
# Needs: the server running in another terminal (scripts/start.sh), `forge` on PATH (the backend venv), git, python3.
# Target repository: by default a fresh local one built by scripts/demo/make_demo_repo.sh. To demo with a repo that
# really lives on GitHub: DEMO_REPO_URL=https://github.com/<you>/forge-demo-shop scripts/demo/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ISSUE=1; MODE=wait; PAUSE_SECONDS="${DEMO_PAUSE_SECONDS:-3}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="${2:?--issue needs 1, 2 or 3}"; shift 2 ;;
    --all)   ISSUE=all; shift ;;
    --auto)  MODE=auto; shift ;;
    --fast)  MODE=fast; shift ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

case "$ISSUE" in
  1) CLASS=Issue1FreeShippingAtThreshold
     GOAL="Issue #1: orders of exactly 50.00 are charged shipping, but the pricing rules (docs/pricing-rules.md) say free shipping starts AT 50.00. Fix the library; do not edit the tests." ;;
  2) CLASS=Issue2VatRounding
     GOAL="Issue #2: VAT is off by a cent on half-cent amounts (5% of 2.50 should be 0.13, we return 0.12). Rounding must follow docs/pricing-rules.md. Fix the library; do not edit the tests." ;;
  3) CLASS=Issue3CouponsDoNotStack
     GOAL="Issue #3: customers who enter two coupons get both discounts, but only the single best coupon may apply (docs/pricing-rules.md). Fix the library; do not edit the tests." ;;
  all) CLASS=""
     GOAL="Three bugs are reported in tests/test_regressions.py: free-shipping threshold, VAT rounding, coupon stacking. The rules are in docs/pricing-rules.md. Fix the library code; do not edit the tests." ;;
  *) echo "--issue must be 1, 2, 3 (or use --all)" >&2; exit 2 ;;
esac
if [[ -n "$CLASS" ]]; then TEST="python -m unittest tests.test_regressions.$CLASS"; else TEST="python -m unittest"; fi

if [[ -t 1 ]]; then B=$'\033[1m'; D=$'\033[2m'; C=$'\033[36m'; Y=$'\033[33m'; R=$'\033[0m'; else B=""; D=""; C=""; Y=""; R=""; fi

scene() {  # scene <number> <title>
  echo; echo "${C}${B}━━ $1 ━ $2 ${R}"; echo
}
say()  { echo "${D}$*${R}"; }
show() { echo "${Y}\$ $*${R}"; }                     # print a command the way a terminal would
pause() {
  case "$MODE" in
    fast) ;;
    auto) sleep "$PAUSE_SECONDS" ;;
    wait) read -r -p "${D}   [Enter] ▸ ${R}" _ ;;
  esac
}

# ── 0. preflight ────────────────────────────────────────────────────────────────────────────────────────────────
for tool in forge git python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing: $tool (activate the backend venv: source backend/.venv/bin/activate)" >&2; exit 3; }
done
if ! forge health >/dev/null 2>&1; then
  echo "The Forge server is not reachable. In ANOTHER terminal run:  scripts/start.sh   (wait for 'Uvicorn running')" >&2
  exit 3
fi

WORK="${DEMO_DIR:-${TMPDIR:-/tmp}/forgemini-demo-$(date -u +%Y%m%dT%H%M%S)}"
REPO="$WORK/forge-demo-shop"
mkdir -p "$WORK"
tests_here() { (cd "$REPO" && python3 -m unittest ${CLASS:+tests.test_regressions.$CLASS} 2>&1 | tail -"${1:-14}") || true; }

# ── 1. someone else's repository ───────────────────────────────────────────────────────────────────────────────
scene "1/5" "Somebody else's repository"
if [[ -n "${DEMO_REPO_URL:-}" ]]; then
  show "git clone $DEMO_REPO_URL"
  git clone --quiet --depth 1 "$DEMO_REPO_URL" "$REPO"
else
  say "(built locally by scripts/demo/make_demo_repo.sh: a small pricing library with a believable history)"
  "$ROOT/scripts/demo/make_demo_repo.sh" "$REPO" >/dev/null
fi
show "cd forge-demo-shop && git log --oneline"
(cd "$REPO" && git --no-pager log --oneline)
echo
show "ls"
(cd "$REPO" && ls)
say "We did not write this code and we have never seen it. Its rules are in docs/pricing-rules.md."
pause

# ── 2. the bug report ──────────────────────────────────────────────────────────────────────────────────────────
scene "2/5" "A bug report arrives"
echo "${B}$GOAL${R}"
echo
show "python3 -m unittest ${CLASS:+tests.test_regressions.$CLASS}"
tests_here 14
say "Red. That is the bug, reproduced by the project's own tests."
pause

# ── 3. ForgeMini ───────────────────────────────────────────────────────────────────────────────────────────────
scene "3/5" "ForgeMini takes the task"
say "The agent works inside a Docker container WITHOUT network. It cannot touch your checkout."
show "forge solve --repo forge-demo-shop --test \"$TEST\" --goal \"…\" --apply"
echo
set +e
forge solve --repo "$REPO" --goal "$GOAL" --test "$TEST" --apply
SOLVE_RC=$?
set -e
pause

# ── 4. the result ──────────────────────────────────────────────────────────────────────────────────────────────
scene "4/5" "What changed in the repository"
show "git diff"
(cd "$REPO" && git --no-pager diff --stat && echo && git --no-pager diff --color="$([[ -t 1 ]] && echo always || echo never)")
show "git status --short tests/"
(cd "$REPO" && git status --short tests/)
say "(nothing listed: the agent did not touch a single test)"
pause

# ── 5. green ───────────────────────────────────────────────────────────────────────────────────────────────────
scene "5/5" "Green"
show "python3 -m unittest ${CLASS:+tests.test_regressions.$CLASS}"
tests_here 6
echo
if [[ $SOLVE_RC -eq 0 ]]; then
  echo "${B}Done.${R} Bug found, fixed and independently verified; the diff above is the only change."
else
  echo "${B}Forge did not verify a fix (exit $SOLVE_RC).${R} That is a legitimate outcome: nothing was applied. See the explanation above."
fi
say "Work directory: $WORK  (rm -rf it when done)"
exit "$SOLVE_RC"
