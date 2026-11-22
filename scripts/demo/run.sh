#!/usr/bin/env bash
# ForgeMini live demo: somebody else's repo, one command, and a verdict you do not have to take on trust.
#
#   scripts/demo/run.sh                   # story "fix": issue #1, press Enter between scenes (live presenting)
#   scripts/demo/run.sh --issue 2         # issue 1, 2 or 3  (each is a separate bug, ~1-2 minutes)
#   scripts/demo/run.sh --all             # all three bugs in one run (whole test suite)
#   scripts/demo/run.sh --story refuse    # story "refuse": a request that contradicts the project's rules
#   scripts/demo/run.sh --cues            # print the presenter's cue sheet for the chosen story and exit
#   scripts/demo/run.sh --auto            # timed pauses instead of Enter (for screen recording / asciinema)
#   scripts/demo/run.sh --fast            # no pauses at all (used by the tests)
#
# Needs: the server running in another terminal (scripts/start.sh), `forge` on PATH (the backend venv), git, python3.
# Target repository: by default a fresh local one built by scripts/demo/make_demo_repo.sh. To demo with a repo that
# really lives on GitHub: DEMO_REPO_URL=https://github.com/<you>/forge-demo-shop scripts/demo/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORY=fix; ISSUE=1; MODE=wait; CUES=0; PAUSE_SECONDS="${DEMO_PAUSE_SECONDS:-3}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="${2:?--issue needs 1, 2 or 3}"; shift 2 ;;
    --all)   ISSUE=all; shift ;;
    --story) STORY="${2:?--story needs fix or refuse}"; shift 2 ;;
    --cues)  CUES=1; shift ;;
    --auto)  MODE=auto; shift ;;
    --fast)  MODE=fast; shift ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

case "$STORY" in
  fix)
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
    if [[ -n "$CLASS" ]]; then TEST="python -m unittest tests.test_regressions.$CLASS"; else TEST="python -m unittest"; fi ;;
  refuse)
    # Cannot be done honestly: docs/pricing-rules.md says "free from 50.00" and tests/test_shipping.py pins 49.99 to the
    # flat rate. The tests are GREEN before any change, so the Verifier can never say PASS here, whatever the model
    # does: moving the threshold turns the 49.99 test red, editing a test is a protected violation, a no-op is
    # inconclusive. The honest outcome does not depend on the model being good.
    TEST="python -m unittest tests.test_shipping"
    GOAL="Request from marketing: starting today, orders of 40.00 or more ship free (the 'free from 40' promo). Change the library; do not edit the tests." ;;
  *) echo "--story must be fix or refuse" >&2; exit 2 ;;
esac

if [[ -t 1 ]]; then B=$'\033[1m'; D=$'\033[2m'; C=$'\033[36m'; Y=$'\033[33m'; R=$'\033[0m'; else B=""; D=""; C=""; Y=""; R=""; fi

# ── presenter's cue sheet: printed BEFORE the show (second screen / phone), never during it, because a shared or
#    projected terminal would show the cues to everybody ───────────────────────────────────────────────────────────
if [[ $CUES -eq 1 ]]; then
  echo "${B}Шпаргалка ведущего — история «$STORY»${R}  (сцены переключаются по Enter)"
  echo
  echo "1/5  Чужой репозиторий. «Это не наш код: маленькая библиотека расчёта цен, у неё своя история и свои правила"
  echo "     в docs/pricing-rules.md. Мы её не писали и не видели»."
  if [[ "$STORY" == fix ]]; then
    echo "2/5  Баг-репорт. «Пришла жалоба. Сначала убеждаемся, что баг настоящий: собственные тесты проекта красные»."
    echo "3/5  ForgeMini. «Агент работает в контейнере без сети и не трогает наш код. Смотрите на последние строки:"
    echo "     агент говорит „готово“ — но это только заявление. Forge сам перезапускает тесты в чистом контейнере»."
    echo "     Карточка: тесты падали до правки, проходят после, тестовые файлы не тронуты, сколько стоило."
    echo "4/5  Что изменилось. «Одна строка в одном файле; тесты агент не трогал — иначе результат бы не засчитали»."
    echo "5/5  Зелёный. «Главное не то, что агент починил, а то, что мы можем это проверить, не веря ему на слово»."
  else
    echo "2/5  Просьба маркетинга: бесплатная доставка от 40. «Но правила проекта говорят 50, и это закреплено"
    echo "     тестом: заказ на 49.99 платит доставку. Выполнить просьбу, не нарушив правил, нельзя»."
    echo "3/5  ForgeMini. «Посмотрим, что сделает агент. Важно: даже если он „всё сделает“, Forge это не засчитает —"
    echo "     до правки тесты зелёные, доказывать нечего, а сдвинутый порог сломает тест на 49.99»."
    echo "4/5  Что изменилось. «Ничего. Непроверенное в код не попадает»."
    echo "5/5  Вывод. «Это решение для продукта — поменять правило и тесты, — а не для агента. Система, которая честно"
    echo "     говорит „не могу“, полезнее системы, которая всегда говорит „готово“»."
  fi
  exit 0
fi

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

# /tmp, not $TMPDIR: on macOS $TMPDIR is /var/folders/xx/…/T/, and that path filled whole lines of the live demo.
WORK="${DEMO_DIR:-/tmp/forgemini-demo-$(date -u +%Y%m%dT%H%M%S)}"
REPO="$WORK/forge-demo-shop"
mkdir -p "$WORK"
# The verdict of the project's own tests, not a raw tail: which tests failed, why, and the totals. (A tail cut the
# live demo at one FAIL block while the summary below it said failures=2.)
tests_here() {
  (cd "$REPO" && $TEST 2>&1 \
     | grep -E '^(FAIL|ERROR): |^[A-Za-z]+Error: |^Ran [0-9]+ test|^(OK|FAILED)' \
     | sed -E 's/^(FAIL|ERROR): ([a-z0-9_]+) .*/\1: \2/') || true
}
TEST="${TEST/#python /python3 }"                    # the host has python3; the sandbox image has python
SANDBOX_TEST="${TEST/#python3 /python }"
[[ -t 1 && "$MODE" != fast ]] && clear      # start the story on a clean screen

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

# ── 2. the request ─────────────────────────────────────────────────────────────────────────────────────────────
if [[ "$STORY" == fix ]]; then
  scene "2/5" "A bug report arrives"
  echo "${B}$GOAL${R}"
  echo
  show "$TEST"
  tests_here
  say "Red. That is the bug, reproduced by the project's own tests."
else
  scene "2/5" "A request that contradicts the rules"
  echo "${B}$GOAL${R}"
  echo
  show "grep -n 'ship free' docs/pricing-rules.md"
  (cd "$REPO" && grep -n 'ship free' docs/pricing-rules.md)
  show "grep -n -B1 '49.99' tests/test_shipping.py"
  (cd "$REPO" && grep -n -B1 '49.99' tests/test_shipping.py)
  show "$TEST"
  tests_here
  say "Green today. The project's own rule and its own test say 50.00: making 40.00 free cannot keep them green."
fi
pause

# ── 3. ForgeMini ───────────────────────────────────────────────────────────────────────────────────────────────
scene "3/5" "ForgeMini takes the task"
say "The agent works inside a Docker container WITHOUT network. It cannot touch your checkout."
# One option per line: the full command no longer wraps in the middle of a word on a 115-column terminal.
echo "${Y}\$ forge solve --repo forge-demo-shop \\${R}"
echo "${Y}      --test \"$SANDBOX_TEST\" \\${R}"
echo "${Y}      --goal \"…\" --apply${R}"
echo
set +e
(cd "$WORK" && forge solve --repo forge-demo-shop --goal "$GOAL" --test "$SANDBOX_TEST" --apply)   # the command shown
SOLVE_RC=$?
set -e
pause

# ── 4. the result ──────────────────────────────────────────────────────────────────────────────────────────────
scene "4/5" "What changed in the repository"
if [[ $SOLVE_RC -eq 0 ]]; then
  say "The patch itself was shown above; here is what --apply did to the checkout."
  show "git status --short"
  (cd "$REPO" && git status --short)
  show "git diff --stat"
  (cd "$REPO" && git --no-pager diff --stat)
  show "git status --short tests/"
  (cd "$REPO" && git status --short tests/)
  say "(nothing listed: the agent did not touch a single test)"
else
  show "git status --short"
  (cd "$REPO" && git status --short)
  say "(nothing listed: an unverified result is never applied)"
fi
pause

# ── 5. the end ─────────────────────────────────────────────────────────────────────────────────────────────────
if [[ "$STORY" == fix ]]; then
  scene "5/5" "Green"
  show "$TEST"
  tests_here
  echo
  if [[ $SOLVE_RC -eq 0 ]]; then
    echo "${B}Done.${R} Bug found, fixed and independently verified; the patch from step 3 is the only change."
  else
    echo "${B}Forge did not verify a fix (exit $SOLVE_RC).${R} That is a legitimate outcome: nothing was applied. See the explanation above."
  fi
  RC=$SOLVE_RC
else
  scene "5/5" "Why \"no\" is the right answer"
  show "$TEST"
  tests_here
  echo
  if [[ $SOLVE_RC -ne 0 ]]; then
    echo "${B}As it should be:${R} not verified, nothing applied, the rules still hold."
    say "This needs a product decision (change the rule AND its tests), not a code fix. A system that can say"
    say "\"I could not\" is more useful than one that always says \"done\"."
    RC=0
  else
    echo "${B}UNEXPECTED:${R} Forge verified a change that contradicts the project's rules. Investigate before trusting it."
    RC=1
  fi
fi
say "Work directory: $WORK  (rm -rf it when done)"
exit "$RC"
