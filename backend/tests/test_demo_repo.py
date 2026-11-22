"""The demo target must stay exactly what the demo promises: real bugs, nothing else broken, a believable history.

No Docker or database needed: plain `python -m unittest` on a copy of examples/forge-demo-shop.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHOP = ROOT / "examples" / "forge-demo-shop"
MAKE_REPO = ROOT / "scripts" / "demo" / "make_demo_repo.sh"


def _unittest(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "unittest", *args], cwd=cwd, capture_output=True, text=True,
                          check=False)


@pytest.fixture
def shop(tmp_path):
    dest = tmp_path / "shop"
    shutil.copytree(SHOP, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def test_exactly_the_three_reported_bugs_fail_and_nothing_else(shop):
    run = _unittest(shop, "-v")
    failed = set(re.findall(r"^(test_\w+) \((tests\.[\w.]+)\) \.\.\. (?:FAIL|ERROR)", run.stderr, re.M))
    assert len(failed) == 6
    assert run.returncode != 0 and "failures=6" in run.stderr
    assert {cls.split(".")[1] for _, cls in failed} == {"test_regressions"}          # old, "normal" tests all pass
    assert {cls.split(".")[2] for _, cls in failed} == {          # "tests.<module>.<Class>[.<method>]"
        "Issue1FreeShippingAtThreshold", "Issue2VatRounding", "Issue3CouponsDoNotStack"}


def test_each_issue_is_red_on_its_own_and_independent_of_the_others(shop):
    for cls in ("Issue1FreeShippingAtThreshold", "Issue2VatRounding", "Issue3CouponsDoNotStack"):
        assert _unittest(shop, f"tests.test_regressions.{cls}").returncode != 0, cls


def test_the_reference_fixes_make_everything_green(shop):
    """Proves the bugs are fixable WITHOUT touching tests (and that the spec in docs/ and the tests agree)."""
    def edit(rel, old, new):
        path = shop / rel
        text = path.read_text()
        assert old in text, rel
        path.write_text(text.replace(old, new, 1))

    edit("shopkit/shipping.py", "order_amount > FREE", "order_amount >= FREE")
    edit("shopkit/tax.py", "from decimal import Decimal\n", "from decimal import Decimal\n\nfrom shopkit.money import money\n")
    edit("shopkit/tax.py", "round(amount * rate, 2)", "money(amount * rate)")
    edit("shopkit/discounts.py",
         "    total = ZERO\n    for coupon in coupons:\n        total += discount_amount(coupon, subtotal)\n    return min(total, subtotal)",
         "    return max((discount_amount(c, subtotal) for c in coupons), default=ZERO)")
    run = _unittest(shop)
    assert run.returncode == 0, run.stderr[-800:]


def test_the_vat_regression_test_is_not_weak(shop):
    """A test that passes under the bug proves nothing: every Issue2 case must fail with banker's rounding."""
    tax = shop / "shopkit" / "tax.py"
    for amount, rate in (("2.50", "0.05"), ("0.30", "0.15")):
        code = ("from decimal import Decimal as D; from shopkit.tax import vat; "
                f"print(vat(D('{amount}'), D('{rate}')))")
        got = subprocess.run([sys.executable, "-c", code], cwd=shop, capture_output=True, text=True, check=True)
        expected = {"2.50": "0.12", "0.30": "0.04"}[amount]                              # banker's result (the bug)
        assert got.stdout.strip() == expected, (amount, got.stdout)
    assert tax.exists()


def test_generated_repo_has_history_and_ends_with_failing_regression_tests(tmp_path):
    dest = tmp_path / "stranger"
    done = subprocess.run(["bash", str(MAKE_REPO), str(dest)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    log = subprocess.run(["git", "-C", str(dest), "log", "--format=%an|%s"], capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert len(log) == 6 and all(line.startswith("Alex Maintainer|") for line in log)
    assert "regression tests" in log[0] and "failing until fixed" in log[0]
    assert subprocess.run(["git", "-C", str(dest), "status", "--porcelain"], capture_output=True, text=True,
                          check=True).stdout == ""
    assert _unittest(dest).returncode != 0
    again = subprocess.run(["bash", str(MAKE_REPO), str(dest)], capture_output=True, text=True, check=False)
    assert again.returncode != 0 and "non-empty" in again.stderr                          # never clobbers a directory
