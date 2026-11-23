import pytest

from app.sandbox.changeset import Changeset, FileEntry, Snapshot
from app.sandbox.verification import (
    CommandRun, SandboxVerification, decide, find_protected_violations, is_protected_path,
)
from app.domain.verification import VerificationVerdict as V


def fe(text="x"):
    return FileEntry(text.encode(), 0o644)


@pytest.mark.parametrize("path", [
    "tests/test_a.py", "test/x.py", "pkg/tests/helpers.py", "src/foo_test.py", "test_foo.py",
    "conftest.py", "pkg/conftest.py", "pytest.ini", "tox.ini", "web/app.test.ts", "web/app.spec.js",
])
def test_protected_paths(path):
    assert is_protected_path(path)


@pytest.mark.parametrize("path", ["calc/ops.py", "src/main.py", "README.md", "latest.py", "contest/solver.py",
                                  "pyproject.toml"])
def test_ordinary_paths_are_not_protected(path):
    assert not is_protected_path(path)


def test_modifying_or_deleting_existing_tests_is_a_violation():
    base = Snapshot({"tests/test_a.py": fe("1"), "tests/test_b.py": fe("2"), "calc/ops.py": fe("c")})
    cs = Changeset(written={"tests/test_a.py": fe("changed"), "calc/ops.py": fe("fixed")},
                   deleted=("tests/test_b.py",))
    assert find_protected_violations(base, cs) == ("tests/test_a.py", "tests/test_b.py")


def test_adding_a_new_ordinary_test_is_allowed_but_new_test_config_is_not():
    base = Snapshot({"calc/ops.py": fe()})
    ok = Changeset(written={"tests/test_new.py": fe("def test(): ..."), "calc/ops.py": fe("y")})
    assert find_protected_violations(base, ok) == ()
    sneaky = Changeset(written={"conftest.py": fe("collect_ignore_glob=['*']")})
    assert find_protected_violations(base, sneaky) == ("conftest.py",)


def run(code, timed_out=False):
    return CommandRun(code, timed_out, "out")


@pytest.mark.parametrize("kw, verdict, reason", [
    (dict(changeset_empty=True, violations=(), patched=None, baseline=None), V.FAIL, "no_changes"),
    (dict(changeset_empty=False, violations=("tests/t.py",), patched=run(0), baseline=run(1)), V.FAIL,
     "protected_files_modified: tests/t.py"),
    (dict(changeset_empty=False, violations=(), patched=run(1), baseline=run(1)), V.FAIL, "tests_failed (exit code 1)"),
    (dict(changeset_empty=False, violations=(), patched=run(124, True), baseline=run(1)), V.FAIL, "tests_timed_out"),
    (dict(changeset_empty=False, violations=(), patched=run(0), baseline=run(0)), V.INCONCLUSIVE, "baseline_already_green"),
    (dict(changeset_empty=False, violations=(), patched=run(0), baseline=None), V.INCONCLUSIVE, "baseline_run_missing"),
    (dict(changeset_empty=False, violations=(), patched=run(0), baseline=run(1)), V.PASS, "tests_fail_before_and_pass_after"),
    (dict(changeset_empty=False, violations=(), patched=run(0), baseline=run(124, True)), V.PASS,
     "tests_fail_before_and_pass_after"),
])
def test_decide(kw, verdict, reason):
    got_verdict, got_reason = decide(**kw)
    assert got_verdict is verdict and got_reason.startswith(reason)


def test_success_is_impossible_without_a_red_baseline_and_a_green_patched_run():
    """Exhaustive: PASS only for (patched green, baseline not green, no violations, non-empty)."""
    import itertools
    runs = [None, run(0), run(1), run(124, True)]
    for empty, viol, patched, baseline in itertools.product(
            (True, False), ((), ("tests/t.py",)), runs, runs):
        v, _ = decide(changeset_empty=empty, violations=viol, patched=patched, baseline=baseline)
        expected = (not empty and not viol and patched is not None and patched.passed
                    and baseline is not None and not baseline.passed)
        assert (v is V.PASS) == expected


def test_payload_roundtrip_truncates_output_tail():
    v = SandboxVerification(V.PASS, "r", "cmd", "img", "a" * 64, ("a.py",), (),
                            CommandRun(0, False, "z" * 9000), CommandRun(1, False, "red"), 123)
    back = SandboxVerification.from_payload(v.to_payload(result_sha256="b" * 64))
    assert back.verdict is V.PASS and back.baseline.exit_code == 1 and len(back.patched.output_tail) == 4000


def test_from_payload_rejects_garbage():
    with pytest.raises(ValueError):
        SandboxVerification.from_payload({"schema_version": 2})
    with pytest.raises(ValueError):
        SandboxVerification.from_payload("nope")


# --- test settings inside pyproject.toml / setup.cfg ---------------------------------------------------------------
# These files are not test files, and the agent may legitimately edit them (a dependency, metadata, lint config).
# But their pytest / coverage / tox sections decide WHICH tests run, so a change there is a protected change.
PYPROJECT = '[project]\nname = "shop"\ndependencies = []\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
SETUP_CFG = "[metadata]\nname = shop\n\n[tool:pytest]\ntestpaths = tests\n"


def _violations(before: dict[str, str], written: dict[str, str], deleted: tuple[str, ...] = ()):
    base = Snapshot({p: fe(t) for p, t in before.items()})
    return find_protected_violations(base, Changeset(written={p: fe(t) for p, t in written.items()}, deleted=deleted))


@pytest.mark.parametrize("new_text", [
    # the bypass this guards against: deselect the failing test, the rest stays green
    PYPROJECT + 'addopts = "--deselect tests/test_shipping.py::test_threshold"\n',
    PYPROJECT.replace('testpaths = ["tests"]', 'testpaths = ["docs"]'),               # collect nothing useful
    PYPROJECT + '\n[tool.coverage.report]\nfail_under = 0\n',                          # lower a coverage gate
    PYPROJECT.replace('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', ""),      # drop the section
], ids=["addopts-deselect", "testpaths", "coverage-gate", "section-removed"])
def test_changing_pytest_settings_in_pyproject_is_a_violation(new_text):
    assert _violations({"pyproject.toml": PYPROJECT}, {"pyproject.toml": new_text}) == ("pyproject.toml",)


def test_other_pyproject_edits_are_allowed():
    edited = PYPROJECT.replace("dependencies = []", 'dependencies = ["requests"]') + "\n[tool.ruff]\nline-length = 100\n"
    assert _violations({"pyproject.toml": PYPROJECT}, {"pyproject.toml": edited}) == ()


def test_reformatting_the_pytest_section_without_changing_values_is_allowed():
    # compared as parsed values, not text: comments and spacing are not a change
    reformatted = PYPROJECT.replace('testpaths = ["tests"]', 'testpaths = [ "tests" ]   # where tests live')
    assert _violations({"pyproject.toml": PYPROJECT}, {"pyproject.toml": reformatted}) == ()


def test_new_or_deleted_pyproject_counts_only_if_it_carries_test_settings():
    assert _violations({}, {"pkg/pyproject.toml": PYPROJECT}) == ("pkg/pyproject.toml",)
    assert _violations({}, {"pyproject.toml": '[project]\nname = "x"\n'}) == ()
    assert _violations({"pyproject.toml": PYPROJECT}, {}, deleted=("pyproject.toml",)) == ("pyproject.toml",)
    assert _violations({"pyproject.toml": '[project]\nname = "x"\n'}, {}, deleted=("pyproject.toml",)) == ()


def test_unparsable_config_after_the_change_is_a_violation():
    # fail-closed: if the file cannot be read, nobody can prove the test settings are intact
    assert _violations({"pyproject.toml": PYPROJECT}, {"pyproject.toml": PYPROJECT + "[tool.pytest\n"}) \
        == ("pyproject.toml",)
    assert _violations({"setup.cfg": SETUP_CFG}, {"setup.cfg": SETUP_CFG + "[tool:pytest]\nx = 1\n"}) \
        == ("setup.cfg",)                                                    # duplicate section


def test_setup_cfg_test_sections_are_protected_and_the_rest_is_not():
    assert _violations({"setup.cfg": SETUP_CFG},
                       {"setup.cfg": SETUP_CFG + "addopts = -k 'not threshold'\n"}) == ("setup.cfg",)
    assert _violations({"setup.cfg": SETUP_CFG},
                       {"setup.cfg": SETUP_CFG + "\n[coverage:report]\nfail_under = 0\n"}) == ("setup.cfg",)
    assert _violations({"setup.cfg": SETUP_CFG},
                       {"setup.cfg": SETUP_CFG.replace("name = shop", "name = shop2")}) == ()


@pytest.mark.parametrize("name", [".pytest.ini", "pytest.toml", ".pytest.toml"])
def test_newer_pytest_config_files_are_protected(name):
    assert _violations({}, {name: "[pytest]\naddopts = -k nothing\n"}) == (name,)
