"""scripts/demo/run.sh, rehearsed with a stand-in `forge` (no server, no LLM): the scenes must hold together,
and a run that did NOT fix anything must be reported as such, not dressed up as a success."""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "scripts" / "demo" / "run.sh"

FIXING_FORGE = textwrap.dedent('''\
    #!/usr/bin/env bash
    [[ "$1" == "health" ]] && exit "${FAKE_HEALTH_RC:-0}"
    shift; while [[ $# -gt 0 ]]; do case "$1" in --repo) REPO="$2"; shift 2;; --test) echo "$2" > "$REPO/../test_cmd"; shift 2;; *) shift;; esac; done
    cd "$REPO" && python3 - <<'PY'
    import pathlib
    def sub(p, a, b):
        f = pathlib.Path(p); s = f.read_text(); assert a in s; f.write_text(s.replace(a, b, 1))
    sub("shopkit/shipping.py", "order_amount > FREE", "order_amount >= FREE")
    sub("shopkit/tax.py", "from decimal import Decimal\\n", "from decimal import Decimal\\n\\nfrom shopkit.money import money\\n")
    sub("shopkit/tax.py", "round(amount * rate, 2)", "money(amount * rate)")
    sub("shopkit/discounts.py", "    total = ZERO\\n    for coupon in coupons:\\n        total += discount_amount(coupon, subtotal)\\n    return min(total, subtotal)",
        "    return max((discount_amount(c, subtotal) for c in coupons), default=ZERO)")
    PY
''')
FAILING_FORGE = textwrap.dedent('''\
    #!/usr/bin/env bash
    [[ "$1" == "health" ]] && exit 0
    echo "RESULT: FAILED - NOT verified"; exit 1
''')


def _run(tmp_path: Path, forge_script: str | None, *args: str, env_extra: dict | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if forge_script is not None:
        fake = bin_dir / "forge"
        fake.write_text(forge_script)
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DEMO_DIR": str(tmp_path / "work"),
           **(env_extra or {})}
    return subprocess.run(["bash", str(RUN), "--fast", *args], env=env, capture_output=True, text=True, check=False,
                          timeout=120)


def test_full_story_in_five_scenes_ends_green(tmp_path):
    done = _run(tmp_path, FIXING_FORGE, "--all")
    assert done.returncode == 0, done.stderr + done.stdout[-800:]
    for scene in ("1/5", "2/5", "3/5", "4/5", "5/5"):
        assert scene in done.stdout
    assert "FAILED (failures=6)" in done.stdout                                   # scene 2: red
    assert "Ran 21 tests" in done.stdout and "OK" in done.stdout.split("5/5")[1]  # scene 5: green
    assert "Done." in done.stdout
    assert (tmp_path / "work" / "test_cmd").read_text().strip() == "python -m unittest"


def test_single_issue_uses_its_own_test_class_and_goal(tmp_path):
    done = _run(tmp_path, FIXING_FORGE, "--issue", "2")
    assert done.returncode == 0, done.stderr + done.stdout[-800:]
    assert (tmp_path / "work" / "test_cmd").read_text().strip() == \
        "python -m unittest tests.test_regressions.Issue2VatRounding"
    assert "Issue #2" in done.stdout and "Issue #1" not in done.stdout
    assert "Ran 2 tests" in done.stdout


def test_when_forge_does_not_verify_a_fix_the_demo_says_so_and_exits_nonzero(tmp_path):
    done = _run(tmp_path, FAILING_FORGE, "--issue", "1")
    assert done.returncode == 1
    assert "did not verify a fix" in done.stdout and "Done." not in done.stdout
    assert "FAILED" in done.stdout.split("5/5")[1]                                # still red: nothing was applied


def test_server_down_is_explained_before_anything_is_created(tmp_path):
    done = _run(tmp_path, FIXING_FORGE, env_extra={"FAKE_HEALTH_RC": "3"})
    assert done.returncode == 3 and "scripts/start.sh" in done.stderr
    assert not (tmp_path / "work").exists()


def test_missing_forge_binary_is_explained(tmp_path):
    env_path = "/usr/bin:/bin"                                                    # no venv, so no forge
    done = subprocess.run(["bash", str(RUN), "--fast"], env={"PATH": env_path, "HOME": str(tmp_path)},
                          capture_output=True, text=True, check=False)
    assert done.returncode == 3 and "missing: forge" in done.stderr


@pytest.mark.parametrize("args", [["--issue", "7"], ["--bogus"], ["--story", "nope"]])
def test_bad_arguments_are_rejected(tmp_path, args):
    assert _run(tmp_path, FIXING_FORGE, *args).returncode == 2


# --- story "refuse": a request that contradicts the project's rules ------------------------------------------------
def test_refuse_story_shows_the_contradiction_and_counts_a_non_verified_run_as_the_right_answer(tmp_path):
    done = _run(tmp_path, FAILING_FORGE, "--story", "refuse")
    assert done.returncode == 0, done.stderr + done.stdout[-800:]
    scene2 = done.stdout.split("2/5")[1].split("3/5")[0]
    assert "Orders of **50.00 or more**" in scene2                                 # the rule, from the docs
    assert 'shipping_cost(Decimal("49.99"))' in scene2                             # and the test that pins it
    assert "Ran 3 tests" in scene2 and "OK" in scene2                               # green before: nothing to prove
    assert "an unverified result is never applied" in done.stdout
    assert "As it should be" in done.stdout and "UNEXPECTED" not in done.stdout


def test_refuse_story_flags_a_verified_run_as_unexpected(tmp_path):
    done = _run(tmp_path, FIXING_FORGE, "--story", "refuse")   # a stand-in forge that "succeeds" anyway
    assert done.returncode == 1 and "UNEXPECTED" in done.stdout
    assert (tmp_path / "work" / "test_cmd").read_text().strip() == "python -m unittest tests.test_shipping"


def test_the_refuse_task_really_is_impossible_to_verify(tmp_path):
    """Not a script property but the story's premise: on the real demo repo the requested change (free from 40.00)
    breaks a test that is green today, so no honest patch can make the Verifier say PASS."""
    repo = tmp_path / "shop"
    subprocess.run(["bash", str(ROOT / "scripts" / "demo" / "make_demo_repo.sh"), str(repo)], check=True,
                   capture_output=True)
    # -B: no __pycache__. Without it this test once PASSED the promo: the edit kept the file size (50 -> 40) within the
    # same second as the first run, and Python reused the stale .pyc (it checks source mtime in seconds + size).
    unittest = ["python3", "-B", "-m", "unittest", "tests.test_shipping"]
    assert subprocess.run(unittest, cwd=repo, capture_output=True).returncode == 0      # green before
    shipping = repo / "shopkit" / "shipping.py"
    shipping.write_text(shipping.read_text().replace('Decimal("50.00")', 'Decimal("40.00")'))
    assert subprocess.run(unittest, cwd=repo, capture_output=True).returncode != 0      # the promo breaks the rule


@pytest.mark.parametrize("story,marker", [("fix", "Баг-репорт"), ("refuse", "Просьба маркетинга")])
def test_cue_sheet_is_printed_before_the_show_and_runs_nothing(tmp_path, story, marker):
    done = subprocess.run(["bash", str(RUN), "--cues", "--story", story], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)                   # no forge on PATH at all
    assert done.returncode == 0 and marker in done.stdout and "5/5" in done.stdout
