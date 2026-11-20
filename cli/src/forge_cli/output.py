"""Minimal terminal-safe plain text and structured JSON output."""

import json

import typer
from pydantic import BaseModel

from forge_cli.dto import WorkspaceResponse


def _escape(value: str) -> str:
    # ASCII JSON escaping covers control characters, ANSI, and Unicode format
    # controls. Escape brackets as well so values cannot become Rich markup.
    escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    return escaped.replace("[", r"\u005b").replace("]", r"\u005d")


def print_result(result: BaseModel | list[WorkspaceResponse], *, json_output: bool) -> None:
    models = result if isinstance(result, list) else [result]
    if json_output:
        payload = [model.model_dump(mode="json") for model in models]
        if not isinstance(result, list):
            payload = payload[0]
        typer.echo(json.dumps(payload, ensure_ascii=True))
        return

    if not models:
        typer.echo("No workspaces found.")
        return

    for model in models:
        for field, value in model.model_dump(mode="json").items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                typer.echo(f"{field}={_escape(item)}")
