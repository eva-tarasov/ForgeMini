"""Terminal-safe presentation for Forge command results."""

import json
import os
import sys
from unicodedata import category
from uuid import UUID

import typer
from pydantic import BaseModel
from rich.cells import chop_cells
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from forge_cli.dto import (
    ApprovalDetailResponse,
    ApprovalDecisionResponse,
    ApprovalPatchReview,
    EvidenceAttemptDetail,
    EvidenceAttemptSummary,
    EvidenceListResponse,
    PendingApprovalListResponse,
    LiveApprovalPreview,
    AgentRunEventsResponse,
    AgentRunPublicEvent,
    AgentRunResponse,
    AgentRunResultResponse,
    AgentRunStartResponse,
    AgentRunStatus,
    RunFailureDetails,
    RunPreliminaryDetails,
    RunTransitionDetails,
    RunStepDetails,
    RunVerificationDetails,
    TaskResponse,
    WorkspaceResponse,
)
from forge_cli.polling import WatchOutcome

# Leave room for a full UUID, labels, and the borders and padding.
_MIN_RICH_WIDTH = 54
_UUID_WIDTH = 36
_UNSAFE_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def stdout_console(*, no_color: bool) -> tuple[Console, bool]:
    """(console, rich_layout): rich_layout is True only on an interactive terminal with color allowed."""
    stream = sys.stdout
    is_tty = stream.isatty()
    # FORCE_COLOR may affect Rich's capability detection even for a pipe.
    allow_color = is_tty and not (no_color or os.environ.get("NO_COLOR"))
    console = Console(
        file=stream,
        color_system="auto" if allow_color else None,
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=True,
    )
    rich_layout = is_tty and console.is_interactive and not console.options.ascii_only
    return console, rich_layout


def _escape(value: str) -> str:
    # ASCII JSON escaping covers control characters, ANSI, and Unicode format
    # controls. Escape brackets as well so values cannot become Rich markup.
    escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    return escaped.replace("[", r"\u005b").replace("]", r"\u005d")


def _terminal_value(value: str) -> str:
    # Distinguish a literal backslash in a path from an escaped control.
    return "".join(
        json.dumps(char, ensure_ascii=True)[1:-1]
        if char == "\\" or category(char) in _UNSAFE_CATEGORIES
        else char
        for char in value
    )


def _fold_lines(lines: list[str], width: int) -> Text:
    # Fold by terminal cells without Rich's word wrapping trimming whitespace.
    folded = [part for line in lines for part in (chop_cells(line, width) or [line])]
    return Text("\n".join(folded), overflow="ignore", no_wrap=True)


def _workspace_lines(workspace: WorkspaceResponse) -> list[str]:
    return [f"ID: {workspace.id}", f"Name: {_terminal_value(workspace.name)}"]


def _workspace_list(workspaces: list[WorkspaceResponse], width: int) -> Table | Text:
    if not workspaces:
        return Text("No workspaces found.")
    if width < _MIN_RICH_WIDTH:
        entries = ["\n".join(_workspace_lines(workspace)) for workspace in workspaces]
        return Text("\n\n".join(entries))

    # Two padded columns and three vertical borders use seven cells.
    name_width = width - _UUID_WIDTH - 7
    table = Table(title="Workspaces", header_style="cyan", title_style="cyan", padding=(0, 1))
    table.add_column("ID", width=_UUID_WIDTH, no_wrap=True)
    table.add_column("Name", width=name_width)
    for workspace in workspaces:
        name = _terminal_value(workspace.name)
        table.add_row(Text(str(workspace.id)), _fold_lines([name], name_width))
    return table


def _event_lines(events: list[AgentRunPublicEvent]) -> list[str]:
    lines = []
    for event in events:
        lines.extend([f"#{event.sequence}  {event.created_at.isoformat()}", f"Kind: {event.kind}"])
        details = event.details
        if isinstance(details, RunTransitionDetails):
            lines.extend([f"From: {details.previous}", f"To: {details.to}"])
        elif isinstance(details, RunPreliminaryDetails):
            lines.append(f"Preliminary status: {details.preliminary_status}")
        elif isinstance(details, RunFailureDetails):
            lines.append(f"Execution failure code: {details.code}")
        elif isinstance(details, RunVerificationDetails):
            lines.append(f"Verification verdict: {details.verdict}")
        elif isinstance(details, RunStepDetails):
            lines.append(f"Step ({details.step}): {details.summary}")
        else:
            lines.append("Details: none")
        lines.append("")
    return lines


def _event_page_lines(page: AgentRunEventsResponse) -> list[str]:
    lines = _event_lines(page.items) if page.items else ["No events on this page."]
    lines.extend([
        f"Next after sequence: {page.next_after_sequence}",
        f"Has more: {json.dumps(page.has_more)}",
    ])
    return lines


def _result_lines(result: AgentRunResultResponse) -> list[str]:
    lines = [
        f"Run ID: {result.run_id}",
        f"Run status: {result.run_status}",
        f"Preliminary status: {result.preliminary_status or 'not recorded'}",
        f"Execution failure code: {result.execution_failure_code or 'not recorded'}",
        f"Verification verdict: {result.verification_verdict or 'not recorded'}",
    ]
    usage = result.usage
    if usage is None:
        lines.extend([
            "Known total tokens: unknown", "Usage complete: unknown",
            "Known cost (USD): unknown", "Cost complete: unknown",
        ])
    else:
        tokens = str(usage.known_total_tokens)
        cost = usage.known_cost_usd if usage.known_cost_usd is not None else "unknown"
        lines.extend([
            f"Known total tokens: {tokens}" + ("" if usage.usage_complete else " (incomplete)"),
            f"Usage complete: {json.dumps(usage.usage_complete)}",
            f"Known cost (USD): {cost}" + ("" if usage.cost_complete else " (incomplete)"),
            f"Cost complete: {json.dumps(usage.cost_complete)}",
        ])
    return lines


def _detail_panel(title: str, lines: list[str], width: int) -> Panel | Text:
    if width < _MIN_RICH_WIDTH:
        return Text("\n".join([title, *lines]))
    return Panel(
        _fold_lines(lines, width - 4),
        title=title,
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    )


def _print_detail_lines(console: Console, rich_layout: bool, title: str, lines: list[str]) -> None:
    if rich_layout:
        presentation = _detail_panel(title, [_terminal_value(line) for line in lines], console.width)
        console.print(presentation, soft_wrap=isinstance(presentation, Text))
    else:
        for line in lines:
            console.print(_escape(line))


def print_watch_page(
    status_change: AgentRunStatus | None, events: list[AgentRunPublicEvent], *, no_color: bool
) -> None:
    lines = [f"Observed status: {status_change}"] if status_change is not None else []
    lines.extend(_event_lines(events))
    if lines:
        console, rich_layout = stdout_console(no_color=no_color)
        _print_detail_lines(console, rich_layout, "AgentRun watch", lines)


def print_watch_summary(
    run_id: UUID,
    outcome: WatchOutcome,
    events: list[AgentRunPublicEvent],
    *,
    json_output: bool,
    no_color: bool,
) -> None:
    if json_output:
        typer.echo(json.dumps({
            "run_id": str(run_id),
            "last_observed_status": outcome.last_observed_status,
            "events": [event.model_dump(mode="json") for event in events],
            "next_after_sequence": outcome.next_after_sequence,
            "stop_reason": outcome.stop_reason,
        }, ensure_ascii=True))
        return

    console, rich_layout = stdout_console(no_color=no_color)
    _print_detail_lines(console, rich_layout, "AgentRun observation ended", [
        f"Run ID: {run_id}",
        f"Last observed status: {outcome.last_observed_status or 'unknown'}",
        f"Next after sequence: {outcome.next_after_sequence}",
        f"Stop reason: {outcome.stop_reason}",
    ])


def _terminal_result(
    result: BaseModel | list[WorkspaceResponse], width: int
) -> Panel | Table | Text | None:
    if isinstance(result, list):
        return _workspace_list(result, width)
    if isinstance(result, WorkspaceResponse):
        title = "Workspace"
        lines = _workspace_lines(result)
    elif isinstance(result, TaskResponse):
        title = "Task"
        lines = [
            f"ID: {result.id}",
            f"Workspace ID: {result.workspace_id}",
            f"Goal: {_terminal_value(result.goal)}",
            "Acceptance criteria:",
        ]
        lines.extend(
            f"{index}. {_terminal_value(criterion)}"
            for index, criterion in enumerate(result.acceptance_criteria, start=1)
        )
    elif isinstance(result, AgentRunResponse):
        title = "AgentRun"
        lines = [
            f"ID: {result.id}",
            f"Task ID: {result.task_id}",
            f"Attempt number: {result.attempt_number}",
            f"Status: {result.status}",
            f"Created at: {result.created_at.isoformat()}",
        ]
    elif isinstance(result, AgentRunStartResponse):
        title = "AgentRun start accepted"
        lines = [f"Run ID: {result.run_id}", "Accepted: true"]
    else:
        return None

    return _detail_panel(title, lines, width)


def _evidence_summary_lines(item: EvidenceAttemptSummary) -> list[str]:
    return [
        f"Attempt ID: {item.attempt_id}", f"Sequence: {item.sequence}",
        f"Verdict: {item.verdict}", f"File verdict: {item.file_verdict}",
        f"Audit complete: {json.dumps(item.audit_complete)}",
        f"Isolated fixture: {json.dumps(item.isolated_fixture)}", f"Check count: {item.check_count}",
    ]


def _capability_lines(result: BaseModel) -> tuple[str, list[str]] | None:
    if isinstance(result, ApprovalDecisionResponse):
        return "Approval decision recorded", [
            f"Run ID: {result.run_id}", f"Approval ID: {result.approval_id}",
            f"Decision: {result.decision}", "Recorded: true",
        ]
    if isinstance(result, PendingApprovalListResponse):
        lines = []
        for item in result.items:
            lines.extend([
                f"ID: {item.id}", f"Run ID: {item.run_id}", f"Tool: {item.tool_name}",
                f"Status: {item.status}", f"Requested at: {item.requested_at.isoformat()}", "",
            ])
        return "Pending approvals", lines or ["No pending approvals."]
    if isinstance(result, ApprovalDetailResponse):
        lines = [
            f"ID: {result.id}", f"Run ID: {result.run_id}", f"Tool: {result.tool_name}",
            f"Status: {result.status}", f"Requested at: {result.requested_at.isoformat()}",
            f"Decided at: {result.decided_at.isoformat() if result.decided_at else 'not recorded'}",
        ]
        if isinstance(result.preview, LiveApprovalPreview):
            lines.extend(["Preview: live at read", "Content included: false"])
            for file in result.preview.files:
                lines.extend([
                    f"Path: {file.path}", f"Added lines: {file.added_lines}",
                    f"Removed lines: {file.removed_lines}", f"Hunk count: {file.hunk_count}",
                ])
        else:
            lines.append("Preview unavailable")
        return "Approval", lines
    if isinstance(result, ApprovalPatchReview):
        return "Exact approval review", [
            f"Run ID: {result.run_id}", f"Approval ID: {result.approval_id}",
            f"SHA256 fingerprint: {result.patch_sha256}",
        ]
    if isinstance(result, EvidenceListResponse):
        lines = [f"Run ID: {result.run_id}"]
        for item in result.items:
            lines.extend([*_evidence_summary_lines(item), ""])
        if not result.items:
            lines.append("No evidence on this page.")
        lines.extend([
            f"Next after sequence: {result.next_after_sequence}", f"Has more: {json.dumps(result.has_more)}",
        ])
        return "Verification evidence", lines
    if isinstance(result, EvidenceAttemptDetail):
        lines = _evidence_summary_lines(result)
        for check in result.checks:
            lines.extend([
                f"Check ID: {check.check_id}", f"Criterion index: {check.criterion_index}",
                f"Path: {check.path}", f"Outcome: {check.outcome}",
            ])
        return "Verification attempt", lines
    return None


def print_result(
    result: BaseModel | list[WorkspaceResponse], *, json_output: bool, no_color: bool = False
) -> None:
    models = result if isinstance(result, list) else [result]
    if json_output:
        payload = [model.model_dump(mode="json") for model in models]
        if not isinstance(result, list):
            payload = payload[0]
        typer.echo(json.dumps(payload, ensure_ascii=True))
        return

    console, rich_layout = stdout_console(no_color=no_color)
    capability = _capability_lines(result)
    if capability is not None:
        title, lines = capability
        _print_detail_lines(console, rich_layout, title, lines)
        if isinstance(result, ApprovalPatchReview):
            escape = _terminal_value if rich_layout else _escape
            patch = "\n".join(escape(line) for line in result.patch.split("\n"))
            console.print(Text(patch, no_wrap=True, overflow="ignore"), soft_wrap=True, end="")
        return
    if isinstance(result, (AgentRunEventsResponse, AgentRunResultResponse)):
        if isinstance(result, AgentRunEventsResponse):
            title, lines = "AgentRun events", _event_page_lines(result)
        else:
            title, lines = "AgentRun result", _result_lines(result)
        _print_detail_lines(console, rich_layout, title, lines)
        return

    if rich_layout:
        presentation = _terminal_result(result, console.width)
        if presentation is not None:
            console.print(presentation, soft_wrap=isinstance(presentation, Text))
            return

    if not models:
        console.print("No workspaces found.")
        return

    for model in models:
        for field, value in model.model_dump(mode="json").items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                text = json.dumps(item) if isinstance(item, bool) else str(item)
                console.print(f"{field}={_escape(text)}")
