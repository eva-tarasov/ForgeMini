"""`forge solve`: one command from "here is a repo, a goal and a test command" to a verified result.

workspace (reused if identical) -> task -> run -> start -> live watch -> report.
Everything the server sends here is UNTRUSTED text (test output, model text): it is stripped of
terminal control characters before it is printed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import (
    AgentRunEventsResponse,
    AgentRunStatus,
    RunReport,
    TaskCreate,
    WorkspaceCreate,
)
from forge_cli.errors import ForgeAPIError
from forge_cli.polling import WatchOutcome, watch_run

EXIT_OK = 0
EXIT_NOT_VERIFIED = 1


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
) -> SolveResult:
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

    emit(f"started; watching (up to {timeout}s)…" if watch else "started.")
    if not watch:
        return SolveResult(None, WatchOutcome(None, 0, "timeout"), str(run.id), 0)

    previous: AgentRunStatus | None = None

    def on_page(status: AgentRunStatus, page: AgentRunEventsResponse) -> None:
        nonlocal previous
        for event in page.items:
            line = _event_line(event)
            if line:
                emit(line)
        previous = status

    outcome = watch_run(client, run.id, interval=interval, timeout=timeout, after_sequence=0, on_page=on_page)
    if outcome.stop_reason != "terminal":
        return SolveResult(None, outcome, str(run.id), 6 if outcome.stop_reason == "timeout" else 130)
    report = client.get_run_report(run.id)
    return SolveResult(report, outcome, str(run.id), EXIT_OK if report.run_status == "succeeded" else EXIT_NOT_VERIFIED)


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


def format_report(report: RunReport, run_id: str) -> str:
    verified = report.run_status == "succeeded"
    lines = ["", "=" * 60,
             "RESULT: " + ("SUCCEEDED - verified by an independent re-run in a clean container" if verified
                           else f"{report.run_status.upper()} - NOT verified")]
    v = report.verification
    if report.agent_summary:
        lines += ["", "Agent says:", "  " + sanitize(report.agent_summary, 1500).replace("\n", "\n  ")]
    if report.blocked_reason:
        lines += ["", "Why it stopped:", "  " + sanitize(report.blocked_reason, 1500).replace("\n", "\n  ")]
    if report.failure_code:
        lines += ["", f"Run failure code: {sanitize(report.failure_code, 80)} (details: see the server log / 'forge run events')"]
    if v is not None:
        lines += ["", f"Verification: {v.verdict} - {sanitize(v.reason, 500)}"]
        if v.changed_paths:
            lines.append("Changed files: " + ", ".join(sanitize(p, 200) for p in v.changed_paths))
        if v.protected_violations:
            lines.append("Forbidden changes (test files): " + ", ".join(sanitize(p, 200) for p in v.protected_violations))
        if v.baseline:
            lines.append(f"Tests WITHOUT the change: exit code {v.baseline.exit_code}")
        if v.patched:
            lines.append(f"Tests WITH the change:    exit code {v.patched.exit_code}"
                         + (" (timed out)" if v.patched.timed_out else ""))
            if v.patched.exit_code != 0:
                lines += ["", "Last test output:", "  " + sanitize(v.patched.output_tail, 1500).replace("\n", "\n  ")]
    if report.usage is not None:
        u = report.usage
        tokens = (f"{u.input_tokens:,} input / {u.output_tokens:,} output tokens"
                  if u.input_tokens or u.output_tokens else "token usage not reported by the provider")
        lines += ["", f"Usage: {u.steps} model calls, {u.tool_calls} tool calls, {tokens}"]
    if report.has_patch:
        lines += ["", f"Patch: forge patch {run_id}   (apply with: git apply or patch -p1)"]
    if not verified:
        hint = _PROVIDER_HINTS.get(report.failure_code or "")
        if hint:
            lines += ["", "What to do: " + hint, f"Details of the run: forge run events {run_id}"]
        else:
            lines += ["", "Options: refine the goal and run again, fix the tests/requirements, or inspect the "
                          f"steps with: forge run events {run_id}"]
    return "\n".join(lines)
