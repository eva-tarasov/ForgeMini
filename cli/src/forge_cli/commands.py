"""Forge command routing over the typed HTTP client."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn, TypeVar
from uuid import UUID

import typer
from pydantic import BaseModel, ValidationError

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import (
    MAX_EVENT_PAGE_SIZE,
    MAX_EVENT_SEQUENCE,
    AgentRunEventsResponse,
    AgentRunPublicEvent,
    AgentRunStatus,
    TaskCreate,
    WorkspaceCreate,
    WorkspaceResponse,
)
from forge_cli.errors import ForgeAPIError, ForgeResponseError, ForgeTransportError
from forge_cli.output import print_result, print_watch_page, print_watch_summary, stdout_console
from forge_cli.polling import watch_run
from forge_cli.apply import ApplyError, apply_patch
from forge_cli.repo_source import RepoSourceError, resolve_repo
from forge_cli.solve import EXIT_APPLY_FAILED, SolveStartError, format_report, sanitize, solve as run_solve
from forge_cli.solve_view import SolveView


workspace_app = typer.Typer(no_args_is_help=True, add_completion=False)
task_app = typer.Typer(no_args_is_help=True, add_completion=False)
run_app = typer.Typer(no_args_is_help=True, add_completion=False)

_RUN_TRANSPORT_HINT = "The request may have taken effect. Check its status with 'forge run get UUID'."
T = TypeVar("T")


@dataclass(frozen=True)
class CommandOptions:
    base_url: str
    json_output: bool
    no_color: bool = False


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise typer.BadParameter("Expected a UUID.") from None


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _with_client(
    options: CommandOptions,
    operation: Callable[[ForgeAPIClient], T],
    *,
    transport_hint: str | None = None,
) -> T:
    try:
        with ForgeAPIClient(options.base_url) as client:
            return operation(client)
    except ValidationError:
        _fail("Invalid input. Check the required fields and their length limits.", 2)
    except ForgeTransportError as exc:
        message = str(exc)
        if transport_hint is not None:
            message = f"{message} {transport_hint}"
        _fail(message, 3)
    except ForgeAPIError as exc:
        _fail(str(exc), 4)
    except ForgeResponseError as exc:
        _fail(str(exc), 5)
    except ValueError:
        _fail("Invalid Forge base URL.", 2)


def _run(
    ctx: typer.Context,
    operation: Callable[[ForgeAPIClient], BaseModel | list[WorkspaceResponse]],
    *,
    transport_hint: str | None = None,
) -> None:
    options: CommandOptions = ctx.obj
    result = _with_client(options, operation, transport_hint=transport_hint)
    print_result(result, json_output=options.json_output, no_color=options.no_color)


def health(ctx: typer.Context) -> None:
    """Show the backend health status."""
    _run(ctx, lambda client: client.get_health())


@workspace_app.command("create")
def create_workspace(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", help="Workspace name, 1 to 100 characters.")],
) -> None:
    """Create a workspace."""
    _run(ctx, lambda client: client.create_workspace(WorkspaceCreate(name=name)))


@workspace_app.command("list")
def list_workspaces(ctx: typer.Context) -> None:
    """List workspaces."""
    _run(ctx, lambda client: client.list_workspaces())


@task_app.command("create")
def create_task(
    ctx: typer.Context,
    workspace_id: Annotated[
        UUID, typer.Option("--workspace-id", parser=_parse_uuid, metavar="UUID", help="Workspace UUID.")
    ],
    goal: Annotated[str, typer.Option("--goal", help="Task goal, 1 to 500 characters.")],
    criterion: Annotated[
        list[str], typer.Option("--criterion", help="Acceptance criterion. Repeat for multiple criteria.")
    ],
    idempotency_key: Annotated[
        UUID | None,
        typer.Option("--idempotency-key", parser=_parse_uuid, metavar="UUID", help="Optional idempotency UUID."),
    ] = None,
) -> None:
    """Create a task with one to ten acceptance criteria."""
    _run(
        ctx,
        lambda client: client.create_task(
            TaskCreate(workspace_id=workspace_id, goal=goal, acceptance_criteria=criterion),
            idempotency_key=idempotency_key,
        ),
    )


@task_app.command("get")
def get_task(
    ctx: typer.Context,
    task_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="Task UUID.")],
) -> None:
    """Retrieve a task by UUID."""
    _run(ctx, lambda client: client.get_task(task_id))


@run_app.command("create")
def create_run(
    ctx: typer.Context,
    task_id: Annotated[
        UUID, typer.Option("--task-id", parser=_parse_uuid, metavar="UUID", help="Task UUID.")
    ],
) -> None:
    """Create or retrieve the initial queued run for a task."""
    _run(ctx, lambda client: client.create_run(task_id))


@run_app.command("get")
def get_run(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
) -> None:
    """Retrieve AgentRun metadata by UUID."""
    _run(ctx, lambda client: client.get_run(run_id))


@run_app.command("start")
def start_run(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
) -> None:
    """Request AgentRun execution and report acceptance."""
    _run(ctx, lambda client: client.start_run(run_id), transport_hint=_RUN_TRANSPORT_HINT)


@run_app.command("cancel")
def cancel_run(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
) -> None:
    """Request cancellation and show cancelled AgentRun metadata."""
    _run(ctx, lambda client: client.cancel_run(run_id), transport_hint=_RUN_TRANSPORT_HINT)


@run_app.command("events")
def run_events(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
    after_sequence: Annotated[
        int, typer.Option(min=0, max=MAX_EVENT_SEQUENCE, help="Return sequences after this cursor.")
    ] = 0,
    limit: Annotated[
        int, typer.Option(min=1, max=MAX_EVENT_PAGE_SIZE, help="Maximum number of events to return.")
    ] = 50,
) -> None:
    """Read one page of public AgentRun events."""
    _run(ctx, lambda client: client.get_run_events(run_id, after_sequence=after_sequence, limit=limit))


@run_app.command("result")
def run_result(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
) -> None:
    """Read the public AgentRun result snapshot."""
    _run(ctx, lambda client: client.get_run_result(run_id))


@run_app.command("watch")
def run_watch(
    ctx: typer.Context,
    run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="UUID", help="AgentRun UUID.")],
    interval: Annotated[int, typer.Option(min=1, max=30, help="Seconds between reads.")] = 2,
    timeout: Annotated[int, typer.Option(min=1, max=600, help="Observation budget in seconds.")] = 120,
    after_sequence: Annotated[
        int, typer.Option(min=0, max=MAX_EVENT_SEQUENCE, help="Observe events after this cursor.")
    ] = 0,
) -> None:
    """Observe AgentRun status and events within a time budget."""
    options: CommandOptions = ctx.obj
    events: list[AgentRunPublicEvent] = []
    previous_status: AgentRunStatus | None = None

    def on_page(status: AgentRunStatus, page: AgentRunEventsResponse) -> None:
        nonlocal previous_status
        if options.json_output:
            events.extend(page.items)
        else:
            print_watch_page(
                status if status != previous_status else None, page.items, no_color=options.no_color
            )
            previous_status = status

    outcome = _with_client(
        options,
        lambda client: watch_run(
            client,
            run_id,
            interval=interval,
            timeout=timeout,
            after_sequence=after_sequence,
            on_page=on_page,
        ),
    )
    if not options.json_output or outcome.stop_reason in ("terminal", "waiting_approval"):
        print_watch_summary(
            run_id, outcome, events, json_output=options.json_output, no_color=options.no_color
        )
    if outcome.stop_reason == "timeout":
        _fail("AgentRun observation deadline expired.", 6)
    if outcome.stop_reason == "interrupted":
        _fail("AgentRun observation interrupted.", 130)


def solve(
    ctx: typer.Context,
    repo: Annotated[str, typer.Option(
        "--repo", help="Project directory, or a git URL (https://…, git@host:owner/repo) to clone first.")],
    goal: Annotated[str, typer.Option("--goal", help="What to fix or build, up to 500 characters.")],
    test: Annotated[str, typer.Option("--test", help="Shell command that exits 0 when the project is correct.")],
    image: Annotated[str | None, typer.Option("--image", help="Docker image (default python:3.12-slim).")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Workspace name (default: directory name).")] = None,
    timeout: Annotated[int, typer.Option(min=10, max=7200, help="Seconds to wait for the run.")] = 900,
    interval: Annotated[int, typer.Option(min=1, max=30, help="Seconds between polls.")] = 2,
    no_watch: Annotated[bool, typer.Option("--no-watch", help="Start the run and return immediately.")] = False,
    apply: Annotated[bool, typer.Option(
        "--apply", help="After a VERIFIED result, apply the patch to the repository with git apply.")] = False,
) -> None:
    """Fix a repository: agent works in a Docker sandbox, then Forge re-runs your tests independently.

    Exit code 0 only when the result was verified; 1 when not verified; 6 on timeout.
    """
    options: CommandOptions = ctx.obj
    try:
        source = resolve_repo(repo)
    except RepoSourceError as exc:
        _fail(str(exc), 2)
    repo_path = source.path
    console, rich_layout = stdout_console(no_color=options.no_color)
    view = SolveView(console) if rich_layout and not options.json_output else None
    if view is not None:
        view.banner(repo_label=source.label, commit=source.commit, goal=goal, test=test)
    elif source.cloned_from:
        typer.echo(f"cloned {source.cloned_from} -> {source.path}", err=options.json_output)

    def operation(client: ForgeAPIClient):
        # Fail fast and clearly BEFORE creating anything: "connection refused" almost always means the
        # server is simply not running, and the generic "request may have taken effect" hint would mislead.
        try:
            client.get_health()
        except ForgeTransportError as exc:
            if exc.reason == "connection":
                _fail(
                    f"Cannot reach the Forge server at {options.base_url}.\n"
                    "Start it in ANOTHER terminal (from the repository root):  scripts/start.sh\n"
                    "and wait for the line 'Uvicorn running on ...'. Then run this command again.",
                    3,
                )
            raise
        return run_solve(
            client, repo=repo_path, goal=goal, test=test, image=image, name=name, timeout=timeout,
            interval=interval, watch=not no_watch,
            emit=(view.note if view else lambda line: typer.echo(sanitize(line, 700), err=options.json_output)),
            on_event=view.event if view else None, with_patch=view is not None or apply,
            on_started=view.started if view and not no_watch else None,
        )

    try:
        result = _with_client(options, operation, transport_hint=_RUN_TRANSPORT_HINT)
    except SolveStartError as exc:
        _fail(str(exc), 4)
    if no_watch:
        return  # ids were already printed by solve(); the run continues on the server
    if result.report is None:
        if result.outcome.stop_reason == "timeout":
            _fail(f"Still running after {timeout}s. Keep watching: forge run watch {result.run_id}", 6)
        if result.outcome.stop_reason == "interrupted":
            _fail(f"Interrupted. The run continues on the server: forge run watch {result.run_id}", 130)
        return
    if options.json_output:
        typer.echo(result.report.model_dump_json())
    elif view is not None:
        view.verdict(result.report, result.run_id, result.patch)
    else:
        typer.echo(format_report(result.report, result.run_id))
    exit_code = result.exit_code
    if apply:
        exit_code = exit_code or _apply_verified_patch(result, repo_path, view, options)
    if exit_code:
        raise typer.Exit(exit_code)


def _apply_verified_patch(result, repo_path: Path, view: SolveView | None, options: CommandOptions) -> int:
    """Only a verified result is ever applied. Returns an exit code (0 = applied)."""
    if result.patch is None:
        message = "nothing to apply: the agent made no changes"
    else:
        try:
            apply_patch(repo_path, result.patch)
        except ApplyError as exc:
            typer.echo(f"Not applied: {sanitize(str(exc), 600)}", err=True)
            return EXIT_APPLY_FAILED
        message = f"applied to {repo_path}  (review: git -C {repo_path} diff)"
    if view is not None:
        view.note(message)
    else:
        typer.echo(message, err=options.json_output)
    return 0


def report(ctx: typer.Context, run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="RUN_UUID")]) -> None:
    """Show what happened in a run and why (verdict, evidence, agent's explanation)."""
    options: CommandOptions = ctx.obj
    data = _with_client(options, lambda client: client.get_run_report(run_id))
    typer.echo(data.model_dump_json() if options.json_output else format_report(data, str(run_id)))


def patch(ctx: typer.Context, run_id: Annotated[UUID, typer.Argument(parser=_parse_uuid, metavar="RUN_UUID")]) -> None:
    """Print the agent's patch as a unified diff (redirect it: forge patch UUID > fix.patch)."""
    options: CommandOptions = ctx.obj
    typer.echo(_with_client(options, lambda client: client.get_run_patch(run_id)), nl=False)
