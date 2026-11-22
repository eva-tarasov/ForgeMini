"""Run one attempt of one task against a running Forge server, and judge it with an independent oracle."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from forge_cli.apply import ApplyError, apply_patch
from forge_cli.client import ForgeAPIClient
from forge_cli.errors import ForgeAPIError, ForgeTransportError
from forge_cli.solve import SolveStartError, solve

from evalkit.model import Attempt, classify, is_false_success
from evalkit.tasks import ORACLES, EvalTask

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")


def copy_repo(task: EvalTask, dest: Path) -> Path:
    shutil.copytree(task.repo_path, dest, ignore=_IGNORE)
    return dest


def oracle_on_tree(task: EvalTask, tree: Path, *, timeout: int = 120) -> str:
    """Run the hidden tests against a repository tree. "pass" | "fail" | "error" (could not be evaluated)."""
    assert task.oracle is not None
    env = {**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"}
    names = [f"{task.oracle.module}.{cls}" for cls in task.oracle.classes]
    try:
        done = subprocess.run([sys.executable, "-m", "unittest", *names], cwd=ORACLES, env=env,
                              capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "error"
    if done.returncode == 0:
        return "pass"
    # "could not even import / find the class" must be loud (a broken oracle), not a quiet failure
    broken = ("ModuleNotFoundError: No module named 'oracle_" in done.stderr
              or "AttributeError: module 'oracle_" in done.stderr)
    return "error" if broken else "fail"


def run_oracle(task: EvalTask, patch: str | None, *, timeout: int = 120) -> str:
    """Apply the agent's patch to a FRESH copy and run hidden tests that the agent never saw.

    Returns "pass" | "fail" | "error" | "n/a". "error" (patch does not apply, oracle crashed) is never treated
    as a pass.
    """
    if task.oracle is None:
        return "n/a"
    if not patch:
        return "error"
    with tempfile.TemporaryDirectory(prefix="forge-eval-oracle-") as tmp:
        tree = copy_repo(task, Path(tmp) / "tree")
        try:
            apply_patch(tree, patch)
        except ApplyError:
            return "error"
        return oracle_on_tree(task, tree, timeout=timeout)


def run_attempt(client: ForgeAPIClient, task: EvalTask, attempt_no: int, *, source: Path,
                timeout: int = 900, interval: int = 2) -> Attempt:
    """`source` is a private copy of the task's repository (Forge never modifies it, so it can be reused)."""
    started = time.monotonic()
    base = Attempt(task_id=task.id, attempt=attempt_no, outcome="infra_error", expect=task.expect)
    try:
        result = solve(client, repo=source, goal=task.goal, test=task.test, image=None, name=f"eval-{task.id}",
                       timeout=timeout, interval=interval, emit=lambda _line: None, with_patch=True)
    except (SolveStartError, ForgeAPIError, ForgeTransportError) as exc:
        base.detail = f"{type(exc).__name__}: {exc}"[:300]
        base.seconds = round(time.monotonic() - started, 1)
        return base

    report = result.report
    base.run_id = result.run_id
    base.outcome = classify(report, stop_reason=result.outcome.stop_reason)
    if report is not None:
        v = report.verification
        base.cheat_attempt = bool(v and v.protected_violations)
        if report.usage is not None:
            base.steps, base.tool_calls = report.usage.steps, report.usage.tool_calls
            base.input_tokens, base.output_tokens = report.usage.input_tokens, report.usage.output_tokens
        base.agent_summary = (report.agent_summary or "")[:400]
        base.detail = (report.failure_code or report.blocked_reason or "")[:300]
    else:
        base.detail = f"no terminal state ({result.outcome.stop_reason})"
    if base.outcome == "succeeded":
        base.oracle = run_oracle(task, result.patch)
    base.false_success = is_false_success(task.expect, base.outcome, base.oracle)
    base.seconds = round(time.monotonic() - started, 1)
    return base
