"""Forge console entry point."""

from importlib.metadata import version
from typing import Annotated

import typer

from forge_cli.commands import CommandOptions, health, task_app, workspace_app
from forge_cli.config import resolve_base_url

app = typer.Typer(
    help="Command-line interface for Forge.",
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_show_locals=False,
)
app.command()(health)
app.add_typer(workspace_app, name="workspace", help="Create and list workspaces.")
app.add_typer(task_app, name="task", help="Create and retrieve tasks.")


def show_version(value: bool) -> None:
    if value:
        typer.echo(version("forge-cli"))
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def configure(
    ctx: typer.Context,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="Forge backend URL. Defaults to FORGE_BASE_URL or http://127.0.0.1:8000.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Write successful results as JSON."),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable color in terminal output."),
    ] = False,
    version_flag: Annotated[
        bool,
        typer.Option("--version", callback=show_version, is_eager=True, help="Show the CLI version."),
    ] = False,
) -> None:
    try:
        ctx.obj = CommandOptions(
            base_url=resolve_base_url(base_url), json_output=json_output, no_color=no_color
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--base-url / FORGE_BASE_URL") from None

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    app()
