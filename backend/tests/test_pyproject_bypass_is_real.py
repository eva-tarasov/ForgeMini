"""The threat behind the pyproject.toml / setup.cfg guard is real, not hypothetical.

With real pytest: a project whose suite is red becomes green when the agent only adds
`addopts = "--deselect ..."` to pyproject.toml, without touching a test file. Verifier's own
evidence (red before, green after, test files untouched) would then say PASS, i.e. a false success.
The second test shows the guard catches exactly this change.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.sandbox.changeset import Snapshot
from app.sandbox.verification import find_protected_violations

PYPROJECT = '[project]\nname = "shop"\nversion = "0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
SNEAKY = PYPROJECT + 'addopts = "--deselect tests/test_shipping.py::test_exactly_fifty_ships_free"\n'


def _repo(root: Path, pyproject: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "shipping.py").write_text("def cost(amount):\n    return 0 if amount > 50 else 5\n")   # the bug: > not >=
    (root / "tests" / "test_shipping.py").write_text(
        "from shipping import cost\n\n"
        "def test_big_order_ships_free():\n    assert cost(80) == 0\n\n"
        "def test_exactly_fifty_ships_free():\n    assert cost(50) == 0\n")
    (root / "pyproject.toml").write_text(pyproject)
    return root


def _pytest(root: Path) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=root,
                          capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(root)}).returncode


def test_deselect_in_pyproject_turns_a_red_suite_green(tmp_path):
    assert _pytest(_repo(tmp_path / "before", PYPROJECT)) == 1        # red: the bug is real
    assert _pytest(_repo(tmp_path / "after", SNEAKY)) == 0            # green: bug still there, test skipped


def test_the_guard_flags_exactly_that_change(tmp_path):
    before = Snapshot.from_directory(_repo(tmp_path / "before", PYPROJECT))
    after = Snapshot.from_directory(_repo(tmp_path / "after", SNEAKY))
    changeset = before.changes_to(after)
    assert changeset.paths == ("pyproject.toml",)                    # no test file touched at all
    assert find_protected_violations(before, changeset) == ("pyproject.toml",)
