"""Pure verification logic: which changes are forbidden, and what the test results mean.

No I/O here, so every rule is unit-tested without Docker or PostgreSQL.

Verdict semantics (the point of the whole product):
  PASS          tests FAIL on the pristine source, PASS with the agent's changeset applied in a
                fresh container, and the agent did not touch the tests themselves.
  FAIL          tests still fail / time out, or the agent edited protected (test) files.
  INCONCLUSIVE  the evidence cannot prove a fix: tests were already green before the change, or
                the verification infrastructure itself broke. Never reported as success.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from app.sandbox.changeset import Changeset, Snapshot
from app.domain.verification import VerificationVerdict

SCHEMA_VERSION = 1
SANDBOX_VERIFICATION_RECORDED = "sandbox_verification_recorded"
OUTPUT_TAIL_CHARS = 4_000

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs"})
# Files that can silently disable or reconfigure tests wherever they appear.
_TEST_CONFIG_NAMES = frozenset({"conftest.py", "pytest.ini", "tox.ini", ".coveragerc", "noxfile.py"})


def is_protected_path(path: str) -> bool:
    """True for files that define what 'the tests pass' means."""
    parts = path.split("/")
    name = parts[-1]
    if any(p in _TEST_DIRS for p in parts[:-1]):
        return True
    if name in _TEST_CONFIG_NAMES:
        return True
    stem = posixpath.splitext(name)[0]
    return name.startswith("test_") or stem.endswith("_test") or ".test." in name or ".spec." in name


def find_protected_violations(base: Snapshot, changeset: Changeset) -> tuple[str, ...]:
    """Protected files the agent modified or deleted, or test-config files it created.

    Adding a NEW ordinary test file is allowed (it cannot make an existing test pass), but a new
    conftest.py / pytest.ini is not: those can skip or rewrite the user's tests.
    """
    bad: set[str] = set()
    for path in changeset.deleted:
        if is_protected_path(path):
            bad.add(path)
    for path in changeset.written:
        if not is_protected_path(path):
            continue
        if path in base.files or posixpath.basename(path) in _TEST_CONFIG_NAMES:
            bad.add(path)
    return tuple(sorted(bad))


@dataclass(frozen=True, slots=True)
class CommandRun:
    exit_code: int
    timed_out: bool
    output_tail: str

    def to_payload(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out,
                "output_tail": self.output_tail[-OUTPUT_TAIL_CHARS:]}

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> CommandRun:
        return cls(int(p["exit_code"]), bool(p["timed_out"]), str(p["output_tail"]))

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def decide(
    *, changeset_empty: bool, violations: tuple[str, ...], patched: CommandRun | None,
    baseline: CommandRun | None,
) -> tuple[VerificationVerdict, str]:
    """Map raw facts to (verdict, machine-readable reason)."""
    if changeset_empty:
        return VerificationVerdict.FAIL, "no_changes"
    if violations:
        return VerificationVerdict.FAIL, "protected_files_modified: " + ", ".join(violations)
    if patched is None:
        return VerificationVerdict.INCONCLUSIVE, "patched_run_missing"
    if patched.timed_out:
        return VerificationVerdict.FAIL, "tests_timed_out"
    if patched.exit_code != 0:
        return VerificationVerdict.FAIL, f"tests_failed (exit code {patched.exit_code})"
    if baseline is None:
        return VerificationVerdict.INCONCLUSIVE, "baseline_run_missing"
    if baseline.passed:
        return (VerificationVerdict.INCONCLUSIVE,
                "baseline_already_green: the tests passed even WITHOUT the change, so they do not prove a fix")
    return VerificationVerdict.PASS, "tests_fail_before_and_pass_after"


@dataclass(frozen=True, slots=True)
class SandboxVerification:
    verdict: VerificationVerdict
    reason: str
    test_command: str
    image: str
    changeset_sha256: str
    changed_paths: tuple[str, ...]
    protected_violations: tuple[str, ...]
    patched: CommandRun | None
    baseline: CommandRun | None
    duration_ms: int

    def to_payload(self, *, result_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "test_command": self.test_command,
            "image": self.image,
            "changeset_sha256": self.changeset_sha256,
            "changed_paths": list(self.changed_paths),
            "protected_violations": list(self.protected_violations),
            "patched": self.patched.to_payload() if self.patched else None,
            "baseline": self.baseline.to_payload() if self.baseline else None,
            "duration_ms": self.duration_ms,
            "result_sha256": result_sha256,
        }

    @classmethod
    def from_payload(cls, p: object) -> SandboxVerification:
        if type(p) is not dict or p.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("invalid_sandbox_verification_payload")
        return cls(
            verdict=VerificationVerdict(p["verdict"]), reason=str(p["reason"]),
            test_command=str(p["test_command"]), image=str(p["image"]),
            changeset_sha256=str(p["changeset_sha256"]),
            changed_paths=tuple(p["changed_paths"]), protected_violations=tuple(p["protected_violations"]),
            patched=CommandRun.from_payload(p["patched"]) if p["patched"] else None,
            baseline=CommandRun.from_payload(p["baseline"]) if p["baseline"] else None,
            duration_ms=int(p["duration_ms"]),
        )
