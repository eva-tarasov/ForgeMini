"""scripts/inventory/coverage_diff.py: the check ADR-003 uses to prove that deleting tests did not lose protection."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inventory" / "coverage_diff.py"
spec = importlib.util.spec_from_file_location("coverage_diff_under_test", SCRIPT)
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


def report(tmp_path, name, files):
    path = tmp_path / name
    path.write_text(json.dumps({"files": {f: {"summary": {"num_statements": n, "missing_lines": m}}
                                          for f, (n, m) in files.items()}}))
    return str(path)


def test_unchanged_or_better_is_fine_and_deleted_modules_are_just_listed(tmp_path):
    before = cd.load(report(tmp_path, "b.json", {"a.py": (10, 1), "b.py": (10, 0), "m1.py": (50, 0)}))
    after = cd.load(report(tmp_path, "a.json", {"a.py": (8, 1), "b.py": (10, 0)}))   # a.py lost 2 COVERED lines: ok
    r = cd.compare(before, after)
    assert r["regressed"] == [] and r["gone"] == ["m1.py"]


def test_more_uncovered_statements_is_a_regression_even_if_percent_looks_ok(tmp_path):
    before = cd.load(report(tmp_path, "b.json", {"a.py": (100, 5)}))       # 95%
    after = cd.load(report(tmp_path, "a.json", {"a.py": (200, 6)}))        # 97%, but one MORE unprotected statement
    assert [x[0] for x in cd.compare(before, after)["regressed"]] == ["a.py"]


def test_percentage_drop_alone_is_not_a_regression(tmp_path):
    """Cutting a COVERED M1 branch out of a kernel module: percent falls, protection does not. Must not raise an alarm."""
    before = cd.load(report(tmp_path, "b.json", {"a.py": (10, 1)}))        # 90%
    after = cd.load(report(tmp_path, "a.json", {"a.py": (5, 1)}))          # 80%, same single unprotected statement
    assert cd.compare(before, after)["regressed"] == []


def test_a_lost_test_shows_up_as_new_uncovered_statements(tmp_path):
    before = cd.load(report(tmp_path, "b.json", {"a.py": (10, 1)}))
    after = cd.load(report(tmp_path, "a.json", {"a.py": (10, 4)}))
    assert cd.compare(before, after)["regressed"] == [("a.py", 1, 4, 90.0, 60.0)]
