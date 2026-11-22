"""The demo scenario end to end: a "stranger's" git repo with real bugs -> ForgeMini over HTTP -> verified patch -> applied.

Real: the demo repository built by scripts/demo/make_demo_repo.sh (with its .git), PostgreSQL, Docker sandbox,
Verifier, read API. Scripted: only the model's moves (the live model is run on the owner's machine).
What this protects: the demo cannot silently rot (e.g. someone "fixes" a bug in examples/ and the demo has
nothing left to fix) and the promise in the README holds: the patch Forge returns applies to the ORIGINAL repo
with plain `git apply` and turns the tests green.
"""
import shutil
import subprocess
import sys
from pathlib import Path

from sandbox_support import tool_turn
from test_forgemini_http_e2e import _db_settings, api, pytestmark, wait_terminal  # noqa: F401  (fixture + skip marks)

ROOT = Path(__file__).resolve().parents[2]
MAKE_REPO = ROOT / "scripts" / "demo" / "make_demo_repo.sh"
ALL = "python -m unittest"
ISSUE2 = "python -m unittest tests.test_regressions.Issue2VatRounding"

FIX_SHIPPING = tool_turn("edit_file", {"path": "shopkit/shipping.py",
                                       "old_str": "if order_amount > FREE_SHIPPING_THRESHOLD:",
                                       "new_str": "if order_amount >= FREE_SHIPPING_THRESHOLD:"})
FIX_VAT = [
    tool_turn("edit_file", {"path": "shopkit/tax.py", "old_str": "from decimal import Decimal\n",
                            "new_str": "from decimal import Decimal\n\nfrom shopkit.money import money\n"}),
    tool_turn("edit_file", {"path": "shopkit/tax.py", "old_str": "    return round(amount * rate, 2)",
                            "new_str": "    return money(amount * rate)"}),
]
FIX_COUPONS = tool_turn("edit_file", {
    "path": "shopkit/discounts.py",
    "old_str": "    total = ZERO\n    for coupon in coupons:\n        total += discount_amount(coupon, subtotal)\n"
               "    return min(total, subtotal)",
    "new_str": "    return max((discount_amount(c, subtotal) for c in coupons), default=ZERO)"})


def _git(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True, check=False)


def _tests(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "unittest", *args], cwd=repo, capture_output=True, text=True,
                          check=False)


def _fresh_demo_repo(project: Path) -> None:
    """Replace the fixture's default project with the real demo repo, built by the real script."""
    shutil.rmtree(project)
    done = subprocess.run(["bash", str(MAKE_REPO), str(project)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_all_three_bugs_fixed_verified_and_patch_applies_with_plain_git(api):
    build, make_run, project = api
    _fresh_demo_repo(project)
    assert _tests(project).returncode != 0                                        # a stranger's repo with red tests
    turns = [tool_turn("bash", {"command": ALL}), FIX_SHIPPING, *FIX_VAT, FIX_COUPONS,
             tool_turn("bash", {"command": ALL}),
             tool_turn("finish", {"status": "done", "summary": "three independent bugs fixed"})]
    with build(turns) as client:
        _, run = make_run(client, ALL)
        assert client.post(f"/agent-runs/{run['id']}/start").status_code == 202
        assert wait_terminal(client, run["id"]) == "succeeded"
        report = client.get(f"/agent-runs/{run['id']}/report").json()
        v = report["verification"]
        assert v["verdict"] == "pass" and v["protected_violations"] == []
        assert v["baseline"]["exit_code"] != 0 and v["patched"]["exit_code"] == 0
        assert sorted(v["changed_paths"]) == ["shopkit/discounts.py", "shopkit/shipping.py", "shopkit/tax.py"]
        patch = client.get(f"/agent-runs/{run['id']}/patch").text

        # the live timeline carries what the rich terminal view needs
        steps = [e["details"] for e in client.get(f"/agent-runs/{run['id']}/events?limit=100").json()["items"]
                 if e["kind"] == "agent_step"]
        assert [d["exit_code"] for d in steps if d.get("tool") == "bash"][0] != 0
        assert {d["target"] for d in steps if d.get("tool") == "edit_file"} == {
            "shopkit/shipping.py", "shopkit/tax.py", "shopkit/discounts.py"}

    # Forge never touched the user's checkout; the patch is what changes it, with nothing but git.
    assert _git(project, "status", "--porcelain").stdout == ""
    assert _git(project, "apply", "--check", "-", stdin=patch).returncode == 0
    assert _git(project, "apply", "-", stdin=patch).returncode == 0
    after = _tests(project)
    assert after.returncode == 0 and "OK" in after.stderr
    assert _git(project, "diff", "--stat", "--", "tests").stdout == ""              # no test file was touched


def test_single_issue_scope_changes_only_its_own_file(api):
    build, make_run, project = api
    _fresh_demo_repo(project)
    turns = [tool_turn("bash", {"command": ISSUE2}), *FIX_VAT, tool_turn("bash", {"command": ISSUE2}),
             tool_turn("finish", {"status": "done", "summary": "vat used banker's rounding"})]
    with build(turns) as client:
        _, run = make_run(client, ISSUE2)
        assert client.post(f"/agent-runs/{run['id']}/start").status_code == 202
        assert wait_terminal(client, run["id"]) == "succeeded"
        v = client.get(f"/agent-runs/{run['id']}/report").json()["verification"]
        assert v["verdict"] == "pass" and v["changed_paths"] == ["shopkit/tax.py"]
        assert v["baseline"]["exit_code"] != 0


def test_agent_that_edits_the_regression_tests_instead_of_the_code_is_rejected(api):
    """The cheat the whole design exists for: make the suite green by gutting the test, not by fixing the bug."""
    build, make_run, project = api
    _fresh_demo_repo(project)
    cheat = tool_turn("edit_file", {"path": "tests/test_regressions.py",
                                    "old_str": "class Issue1FreeShippingAtThreshold(unittest.TestCase):",
                                    "new_str": "class _Issue1Disabled:"})
    turns = [cheat, tool_turn("finish", {"status": "done", "summary": "all good"})]
    issue1 = "python -m unittest tests.test_regressions.Issue1FreeShippingAtThreshold"
    with build(turns) as client:
        _, run = make_run(client, issue1)
        assert client.post(f"/agent-runs/{run['id']}/start").status_code == 202
        assert wait_terminal(client, run["id"]) == "failed"
        report = client.get(f"/agent-runs/{run['id']}/report").json()
        assert report["task_status"] != "succeeded"
        assert report["verification"]["verdict"] != "pass"
        assert report["verification"]["protected_violations"] == ["tests/test_regressions.py"]
