"""Workspace and Task command routing over the typed HTTP client."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, NoReturn
from uuid import UUID

import typer
from pydantic import BaseModel, ValidationError

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import TaskCreate, WorkspaceCreate, WorkspaceResponse
from forge_cli.errors import ForgeAPIError, ForgeResponseError, ForgeTransportError
from forge_cli.output import print_result

workspace_app = typer.Typer(no_args_is_help=True, add_completion=False)
task_app = typer.Typer(no_args_is_help=True, add_completion=False)


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


def _run(
    ctx: typer.Context,
    operation: Callable[[ForgeAPIClient], BaseModel | list[WorkspaceResponse]],
) -> None:
    options: CommandOptions = ctx.obj
    try:
        with ForgeAPIClient(options.base_url) as client:
            result = operation(client)
    except ValidationError:
        _fail("Invalid input. Check the required fields and their length limits.", 2)
    except ForgeTransportError as exc:
        _fail(str(exc), 3)
    except ForgeAPIError as exc:
        _fail(str(exc), 4)
    except ForgeResponseError as exc:
        _fail(str(exc), 5)
    except ValueError:
        _fail("Invalid Forge base URL.", 2)

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
