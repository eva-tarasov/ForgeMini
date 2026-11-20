"""Terminal-safe presentation for Forge command results."""

import json
import os
import sys
from unicodedata import category

import typer
from pydantic import BaseModel
from rich.cells import chop_cells
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from forge_cli.dto import TaskResponse, WorkspaceResponse

# Leave room for a full UUID, labels, and the borders and padding.
_MIN_RICH_WIDTH = 54
_UUID_WIDTH = 36
_UNSAFE_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _stdout_console(*, no_color: bool) -> tuple[Console, bool]:
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
    else:
        return None

    if width < _MIN_RICH_WIDTH:
        return Text("\n".join([title, *lines]))
    return Panel(
        _fold_lines(lines, width - 4),
        title=title,
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    )


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

    console, rich_layout = _stdout_console(no_color=no_color)
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
                console.print(f"{field}={_escape(item)}")
