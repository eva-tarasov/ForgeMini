"""Forge console entry point."""

from importlib.metadata import version
from typing import Annotated

import typer

from forge_cli.config import resolve_base_url

app = typer.Typer(
    help="Command-line interface for Forge.",
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


def show_version(value: bool) -> None:
    if value:
        typer.echo(version("forge-cli"))
        raise typer.Exit()


@app.callback(invoke_without_command=True, subcommand_metavar="")
def configure(
    ctx: typer.Context,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="Forge backend URL. Defaults to FORGE_BASE_URL or http://127.0.0.1:8000.",
        ),
    ] = None,
    version_flag: Annotated[
        bool,
        typer.Option("--version", callback=show_version, is_eager=True, help="Show the CLI version."),
    ] = False,
) -> None:
    try:
        ctx.obj = resolve_base_url(base_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--base-url / FORGE_BASE_URL") from None

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    app()
