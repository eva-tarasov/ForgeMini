#!/usr/bin/env bash
# Build the demo target as a standalone git repository with a believable history.
#
#   scripts/demo/make_demo_repo.sh <dest-dir>
#
# Source files live in examples/forge-demo-shop/ (plain files, no nested .git, so they can sit inside this repo).
# The resulting repository is "somebody else's project" for the demo: a small pricing library whose last commit adds
# failing regression tests for three reported bugs. To publish it as its own GitHub repository:
#   cd <dest-dir> && gh repo create forge-demo-shop --private --source=. --push
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../examples/forge-demo-shop" && pwd)"
DEST="${1:?usage: make_demo_repo.sh <dest-dir>}"

if [[ -e "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
  echo "refusing to touch a non-empty directory: $DEST" >&2; exit 1
fi
mkdir -p "$DEST"
cp -R "$SRC/." "$DEST/"
cd "$DEST"
rm -rf .git
find . -name __pycache__ -type d -prune -exec rm -rf {} +

git -c init.defaultBranch=main init -q
git config user.name  "Alex Maintainer"
git config user.email "maintainer@example.com"

commit() {  # commit <iso-date> <message> <paths...>
  local when="$1" msg="$2"; shift 2
  git add -- "$@"
  GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" git commit -q -m "$msg"
}

commit "2026-09-02T10:14:00+00:00" "Initial import: money helpers" \
  .gitignore pyproject.toml README.md shopkit/__init__.py shopkit/money.py tests/__init__.py tests/test_money.py
commit "2026-09-09T16:41:00+00:00" "Add flat-rate shipping with a free-shipping threshold" \
  shopkit/shipping.py tests/test_shipping.py
commit "2026-09-18T09:03:00+00:00" "Add VAT calculation" \
  shopkit/tax.py tests/test_tax.py
commit "2026-09-30T13:27:00+00:00" "Add coupons (percent and fixed)" \
  shopkit/discounts.py tests/test_discounts.py
commit "2026-10-12T11:55:00+00:00" "Add cart and checkout, document pricing rules" \
  shopkit/cart.py tests/test_cart.py docs/pricing-rules.md
commit "2026-11-03T18:20:00+00:00" "Add regression tests for #1, #2, #3 (failing until fixed)" \
  tests/test_regressions.py

git status --porcelain | grep . && { echo "untracked leftovers!" >&2; exit 1; } || true
echo "demo repository ready: $DEST ($(git rev-parse --short HEAD), $(git rev-list --count HEAD) commits)"
