"""`forge solve`: one command from "here is a repo, a goal and a test command" to a verified result.

workspace (reused if identical) -> task -> run -> start -> live watch -> report.
Everything the server sends here is UNTRUSTED text (test output, model text): it is stripped of
terminal control characters before it is printed.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import (
    AgentRunEventsResponse,
    AgentRunStatus,
    RunReport,
    TaskCreate,
    UsageView,
    WorkspaceCreate,
)
from forge_cli.errors import ForgeAPIError
from forge_cli.polling import WatchOutcome, watch_run

EXIT_OK = 0
EXIT_NOT_VERIFIED = 1
EXIT_APPLY_FAILED = 7




_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_UNITTEST_FAILED = re.compile(r"^FAILED \(([^)]*)\)", re.M)
_PYTEST_SUMMARY = re.compile(r"^=+ (.*?) in [\d.]+s.*=+$", re.M)


def count_tests(output_tail: str) -> tuple[int, int] | None:
    """(failed, total) from the test runner's OWN summary line, or None if there is none we recognise.

    unittest: "Ran 2 tests ..." + "OK" / "FAILED (failures=2, errors=1)"; pytest: "=== 2 failed, 3 passed in 0.1s ===".
    Errors count as failed. Anything unrecognised returns None and the caller falls back to the exit code: a number
    we guessed would be worse than no number.
    """
    ran = _UNITTEST_RAN.findall(output_tail)
    if ran:
        total = int(ran[-1])
        failed_line = _UNITTEST_FAILED.findall(output_tail)
        failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", failed_line[-1])) if failed_line else 0
        return min(failed, total), total
    summary = _PYTEST_SUMMARY.findall(output_tail)
    if summary:
        counts = {kind: int(n) for n, kind in re.findall(r"(\d+) (failed|passed|errors?)", summary[-1])}
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        total = failed + counts.get("passed", 0)
        return (failed, total) if total else None
    return None


def describe_test_run(run) -> str:
    """'2 of 2 failed (exit 1)' when the summary is readable, else 'exit 1' — the verdict wording never depends on it."""
    counts = count_tests(run.output_tail or "")
    timed_out = " (timed out)" if run.timed_out else ""
    if counts is None:
        return f"exit {run.exit_code}{timed_out}"
    failed, total = counts
    if total == 0:
        return f"no tests ran (exit {run.exit_code}){timed_out}"
    if failed:
        return f"{failed} of {total} failed (exit {run.exit_code}){timed_out}"
    return f"all {total} passed (exit {run.exit_code}){timed_out}"

def cost_text(usage: UsageView) -> str:
    """Human cost line. Unknown is never shown as $0: the status says why the number is missing or only a floor."""
    def money(value: str) -> str:
        amount = Decimal(value)
        return f"${amount:.4f}" if amount >= Decimal("0.0001") or amount == 0 else "< $0.0001"
    if usage.cost_status == "estimated" and usage.known_cost_usd is not None:
        text = money(usage.known_cost_usd)
        return text if text.startswith("<") else f"≈ {text}"
    if usage.cost_status == "incomplete":
        if usage.known_cost_usd is None:
            return "unknown (the provider did not report usage for the model calls)"
        return f"at least {money(usage.known_cost_usd)} (some model calls could not be priced)"
    if usage.cost_status == "not_priced":
        return "not priced (set FORGE_PRICE_INPUT/OUTPUT_USD_PER_MILLION in the server .env)"
    return ""

def sanitize(text: str, limit: int = 4000) -> str:
    """Drop control characters (ANSI escapes included) except newline and tab."""
    cleaned = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


@dataclass(frozen=True)
class SolveResult:
    report: RunReport | None
    outcome: WatchOutcome
    run_id: str
    exit_code: int
    patch: str | None = None        # filled only when the caller asked for it (the rich view shows the diff)


def ensure_workspace(client: ForgeAPIClient, *, repo: Path, test: str, image: str | None, name: str | None):
    """Reuse a workspace with identical configuration instead of piling up duplicates."""
    source = str(repo)
    for existing in client.list_workspaces():
        if existing.source_path == source and existing.test_command == test and existing.image == image:
            return existing
    return client.create_workspace(WorkspaceCreate(
        name=(name or repo.name or "project")[:100], source_path=source, test_command=test, image=image))


def solve(
    client: ForgeAPIClient, *, repo: Path, goal: str, test: str, image: str | None, name: str | None,
    timeout: int, interval: int, emit: Callable[[str], None], watch: bool = True,
    on_event: Callable[[object], None] | None = None, with_patch: bool = False,
    on_started: Callable[[], None] | None = None,
) -> SolveResult:
    """`on_event` (if given) receives each public event instead of the plain text line."""
    workspace = ensure_workspace(client, repo=repo, test=test, image=image, name=name)
    task = client.create_task(TaskCreate(
        workspace_id=workspace.id, goal=goal, acceptance_criteria=[_criterion(test)]))
    run = client.create_run(task.id)
    emit(f"workspace {workspace.id}\ntask      {task.id}\nrun       {run.id}")
    try:
        client.start_run(run.id)
    except ForgeAPIError as exc:
        if exc.status_code == 503:
            raise SolveStartError(
                "The server cannot start runs. Most likely OPENAI_API_KEY / FORGE_MODEL_NAME are not set "
                "in the server's environment (.env), or Docker is not available. See the server log.") from None
        raise

    if on_started is not None:
        on_started()
    else:
        emit(f"started; watching (up to {timeout}s)…" if watch else "started.")
    if not watch:
        return SolveResult(None, WatchOutcome(None, 0, "timeout"), str(run.id), 0)

    previous: AgentRunStatus | None = None

    def on_page(status: AgentRunStatus, page: AgentRunEventsResponse) -> None:
        nonlocal previous
        for event in page.items:
            if on_event is not None:
                on_event(event)
                continue
            line = _event_line(event)
            if line:
                emit(line)
        previous = status

    outcome = watch_run(client, run.id, interval=interval, timeout=timeout, after_sequence=0, on_page=on_page)
    if outcome.stop_reason != "terminal":
        return SolveResult(None, outcome, str(run.id), 6 if outcome.stop_reason == "timeout" else 130)
    report = client.get_run_report(run.id)
    patch = client.get_run_patch(run.id) if with_patch and report.has_patch else None
    return SolveResult(report, outcome, str(run.id), EXIT_OK if report.run_status == "succeeded" else EXIT_NOT_VERIFIED,
                       patch)


def _criterion(test: str) -> str:
    """Default acceptance criterion; the API caps one criterion at 300 characters."""
    shown = test if len(test) <= 250 else test[:249] + "…"
    return f"`{shown}` passes"


class SolveStartError(Exception):
    pass


def _event_line(event) -> str | None:
    kind = event.kind
    if kind == "agent_step":
        return "  " + sanitize(event.details.summary, 600).replace("\n", " ")
    if kind == "agent_run_status_changed":
        return f"[{event.details.previous} -> {event.details.to}]"
    if kind == "verification_recorded":
        return f"[verification: {event.details.verdict}]"
    return None


# What to do next depends on WHY the run failed: a provider problem is not a problem with the task.
_PROVIDER_HINTS = {
    "provider_auth": "OpenAI rejected the API key. Check OPENAI_API_KEY in .env (scripts/setup.sh), then restart the server.",
    "provider_connection": "The server could not reach OpenAI. Check your internet connection / proxy / VPN.",
    "provider_timeout": "OpenAI did not answer in time. Try again; raise FORGE_MODEL_TIMEOUT_SECONDS in .env if it repeats.",
    "provider_rate_limited": "OpenAI rate limit hit. Wait a minute and try again.",
    "provider_rate_limit_unknown": "OpenAI rate limit hit. Wait a minute and try again.",
    "provider_quota_exhausted": "OpenAI quota/billing is exhausted for this key. Check your OpenAI account.",
    "provider_request_rejected": "OpenAI rejected the request. Most often FORGE_MODEL_NAME is wrong or not available to your key.",
    "provider_unavailable": "OpenAI is temporarily unavailable. Try again later.",
    "provider_error": "OpenAI returned an error. See the server log (terminal 1) for details.",
    "sandbox_error": "The Docker sandbox failed. Is Docker running? See the server log (terminal 1).",
    "workspace_not_configured": "The workspace has no source path / test command (create it via forge solve).",
}


_STOP_CODES = {
    "agent_cannot_fix": "the agent says it cannot do this; its reasons are above",
}


def stop_reason_text(report: RunReport) -> str | None:
    """'Why it stopped', without repeating 'Agent says'. The server stores e.g. "agent_cannot_fix: <the agent's own
    words>" (the full form stays in the API); on screen the same paragraph appeared twice in the live refuse demo."""
    reason = report.blocked_reason
    if not reason:
        return None
    code, sep, rest = reason.partition(": ")
    summary = (report.agent_summary or "").strip()
    if summary and reason.strip() == summary:
        return None                                   # nothing beyond what "Agent says" already shows
    if sep and summary and rest.strip() == summary:
        return f"{code} ({_STOP_CODES.get(code, 'details above')})"
    return reason

def format_report(report: RunReport, run_id: str) -> str:
    verified = report.run_status == "succeeded"
    lines = ["", "=" * 60,
             "RESULT: " + ("SUCCEEDED - verified by an independent re-run in a clean container" if verified
                           else f"{report.run_status.upper()} - NOT verified")]
    v = report.verification
    if report.agent_summary:
        lines += ["", "Agent says:", "  " + sanitize(report.agent_summary, 1500).replace("\n", "\n  ")]
    stop = stop_reason_text(report)
    if stop:
        lines += ["", "Why it stopped:", "  " + sanitize(stop, 1500).replace("\n", "\n  ")]
    if report.failure_code:
        lines += ["", f"Run failure code: {sanitize(report.failure_code, 80)} (details: see the server log / 'forge run events')"]
    if v is not None:
        lines += ["", f"Verification: {v.verdict} - {sanitize(v.reason, 500)}"]
        if v.changed_paths:
            lines.append("Changed files: " + ", ".join(sanitize(p, 200) for p in v.changed_paths))
        if v.protected_violations:
            lines.append("Forbidden changes (test files): " + ", ".join(sanitize(p, 200) for p in v.protected_violations))
        if v.baseline:
            lines.append(f"Tests WITHOUT the change: {describe_test_run(v.baseline)}")
        if v.patched:
            lines.append(f"Tests WITH the change:    {describe_test_run(v.patched)}")
            if v.patched.exit_code != 0:
                lines += ["", "Last test output:", "  " + sanitize(v.patched.output_tail, 1500).replace("\n", "\n  ")]
    if report.usage is not None:
        u = report.usage
        tokens = (f"{u.input_tokens:,} input / {u.output_tokens:,} output tokens"
                  if u.input_tokens or u.output_tokens else "token usage not reported by the provider")
        lines += ["", f"Usage: {u.steps} model calls, {u.tool_calls} tool calls, {tokens}"]
        cost = cost_text(u)
        if cost:
            lines.append(f"Cost: {cost}")
    if report.has_patch:
        lines += ["", f"Patch: forge patch {run_id}   (apply with: git apply or patch -p1)"]
    if not verified:
        lines += ["", *next_step_lines(report, run_id)]
    return "\n".join(lines)


def next_step_lines(report: RunReport, run_id: str) -> list[str]:
    """What to do after a run that is not verified: depends on WHY (a provider problem is not the task's fault)."""
    hint = _PROVIDER_HINTS.get(report.failure_code or "")
    if hint:
        return ["What to do: " + hint, f"Details of the run: forge run events {run_id}"]
    return ["Options: refine the goal and run again, fix the tests/requirements, or inspect the "
            f"steps with: forge run events {run_id}"]
