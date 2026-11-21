import json
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from uuid import UUID

import pytest
from rich.cells import cell_len

from forge_cli import output
from forge_cli.dto import (
    AgentRunEventsResponse,
    AgentRunResponse,
    AgentRunResultResponse,
    AgentRunStartResponse,
    HealthResponse,
    TaskResponse,
    WorkspaceResponse,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
WORKSPACE = WorkspaceResponse(id=WORKSPACE_ID, name="é [bold]\x1b[2J\r\n\t\x00\x85\u202e")
TASK = TaskResponse(
    id=TASK_ID, workspace_id=WORKSPACE_ID, goal="True :rocket: a goal wider than the terminal  ",
    acceptance_criteria=["first", "second"],
)
WORKSPACE_JSON = (
    br'{"id": "00000000-0000-0000-0000-000000000001", '
    br'"name": "\u00e9 [bold]\u001b[2J\r\n\t\u0000\u0085\u202e"}'
)


@pytest.fixture
def render_terminal(monkeypatch, capsys):
    class Terminal(StringIO):
        def __init__(self, encoding):
            super().__init__()
            self.terminal_encoding = encoding

        @property
        def encoding(self):
            return self.terminal_encoding

        def isatty(self):
            return True

    monkeypatch.setenv("TERM", "xterm-256color")
    for variable in ("NO_COLOR", "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(variable, raising=False)

    def render(result, *, width=80, no_color=True, encoding="utf-8"):
        monkeypatch.setenv("COLUMNS", str(width))
        stream = Terminal(encoding)
        with redirect_stdout(stream):
            output.print_result(result, json_output=False, no_color=no_color)
        assert capsys.readouterr() == ("", "")
        return stream.getvalue()

    return render


def test_workspace_table(render_terminal):
    workspaces = [
        WorkspaceResponse(id=WORKSPACE_ID, name="Проект Форж"),
        WorkspaceResponse(id=TASK_ID, name="日本語"),
    ]

    rendered = render_terminal(workspaces)

    assert "Workspaces" in rendered
    assert "┏" in rendered
    assert any("ID" in line and "Name" in line for line in rendered.splitlines())
    assert any(str(WORKSPACE_ID) in line and "Проект Форж" in line for line in rendered.splitlines())
    assert any(str(TASK_ID) in line and "日本語" in line for line in rendered.splitlines())


@pytest.mark.parametrize(
    "no_color, env, has_color",
    [
        (False, {}, True),
        (False, {"NO_COLOR": ""}, True),
        (False, {"NO_COLOR": "0"}, False),
        (False, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, False),
        (True, {"FORCE_COLOR": "1"}, False),
    ],
)
def test_tty_color_policy(render_terminal, monkeypatch, no_color, env, has_color):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    workspace = WorkspaceResponse(id=WORKSPACE_ID, name="Проект Форж")

    rendered = render_terminal([workspace], no_color=no_color)

    assert ("\x1b[" in rendered) is has_color
    assert "┏" in rendered
    assert str(WORKSPACE_ID) in rendered
    assert "Проект Форж" in rendered


@pytest.mark.parametrize("result", [WORKSPACE, TASK], ids=["workspace", "task"])
def test_panel_color_only_changes_styling(render_terminal, result):
    colored = render_terminal(result, no_color=False)
    plain = render_terminal(result, no_color=True)

    assert "\x1b[" in colored
    assert "\x1b[2J" not in colored
    assert "\x1b" not in plain
    assert "╭─" in colored and "╭─" in plain
    assert str(result.id) in colored and str(result.id) in plain


def test_workspace_panel(render_terminal):
    rendered = render_terminal(WorkspaceResponse(id=WORKSPACE_ID, name="Проект Форж"))

    assert "╭─ Workspace " in rendered
    assert f"ID: {WORKSPACE_ID}" in rendered
    assert "Name: Проект Форж" in rendered


def test_task_panel_numbers_criteria(render_terminal):
    task = TASK.model_copy(update={
        "goal": "Проверить сборку",
        "acceptance_criteria": ["Тесты пройдены", "Артефакт создан"],
    })

    rendered = render_terminal(task)

    assert "╭─ Task " in rendered
    assert f"ID: {TASK_ID}" in rendered
    assert f"Workspace ID: {WORKSPACE_ID}" in rendered
    assert "Goal: Проверить сборку" in rendered
    assert rendered.index("Acceptance criteria:") < rendered.index("1. Тесты пройдены")
    assert rendered.index("1. Тесты пройдены") < rendered.index("2. Артефакт создан")


@pytest.mark.parametrize("width", [80, 24])
@pytest.mark.parametrize("status", ["waiting_approval", "cancelled"])
def test_agent_run_presentation_without_color(render_terminal, width, status):
    run = AgentRunResponse(
        id=UUID(int=3),
        task_id=TASK_ID,
        attempt_number=2,
        status=status,
        created_at=datetime.fromisoformat("2026-11-21T09:30:00+03:00"),
    )

    rendered = render_terminal(run, width=width, no_color=True)

    if width == 80:
        assert "╭─ AgentRun " in rendered
    else:
        assert rendered.startswith("AgentRun\n")
    assert f"ID: {run.id}" in rendered
    assert f"Task ID: {TASK_ID}" in rendered
    assert "Attempt number: 2" in rendered
    assert f"Status: {status}" in rendered
    assert "Created at: 2026-11-21T09:30:00+03:00" in rendered
    assert "\x1b" not in rendered


@pytest.mark.parametrize("width", [80, 24])
def test_run_start_acceptance_presentation_without_color(render_terminal, width):
    response = AgentRunStartResponse(run_id=TASK_ID, accepted=True)

    rendered = render_terminal(response, width=width, no_color=True)

    assert "AgentRun start accepted" in rendered
    assert f"Run ID: {TASK_ID}" in rendered
    assert "Accepted: true" in rendered
    assert "\x1b" not in rendered
    assert "running" not in rendered
    assert "succeeded" not in rendered
    if width == 80:
        assert "╭─ AgentRun start accepted " in rendered


@pytest.mark.parametrize("width", [80, 24])
def test_run_events_timeline_without_color(render_terminal, width):
    descriptions = [
        ("agent_run_status_changed", {"from": "queued", "to": "running"}, "From: queued"),
        ("agent_result_recorded", {"preliminary_status": "blocked"}, "Preliminary status: blocked"),
        ("agent_execution_failed", {"code": "execution_failed"}, "Execution failure code: execution_failed"),
        ("verification_recorded", {"verdict": "fail"}, "Verification verdict: fail"),
        ("tool_call_finished", {}, "Details: none"),
    ]
    items = [
        {"sequence": 2147483640 + index, "created_at": "2026-11-21T09:30:00+03:00",
         "kind": kind, "details": details}
        for index, (kind, details, _) in enumerate(descriptions)
    ]
    page = AgentRunEventsResponse.model_validate_json(json.dumps({
        "items": items, "next_after_sequence": 2147483644, "has_more": True,
    }))

    rendered = render_terminal(page, width=width, no_color=True)

    assert "AgentRun events" in rendered
    assert ("╭─ AgentRun events " in rendered) is (width == 80)
    assert "\x1b" not in rendered
    assert "To: running" in rendered
    for index, (kind, _, detail) in enumerate(descriptions):
        assert f"#{2147483640 + index}  2026-11-21T09:30:00+03:00" in rendered
        assert f"Kind: {kind}" in rendered
        assert detail in rendered
    assert "Next after sequence: 2147483644" in rendered
    assert "Has more: true" in rendered


def test_empty_run_events_page_does_not_imply_completion(capsys):
    page = AgentRunEventsResponse(items=[], next_after_sequence=16, has_more=False)

    output.print_result(page, json_output=False)

    assert capsys.readouterr() == (
        "No events on this page.\nNext after sequence: 16\nHas more: false\n", ""
    )


@pytest.mark.parametrize("width", [80, 24])
@pytest.mark.parametrize("has_usage", [False, True])
def test_run_result_presentation_distinguishes_unknown_from_zero(render_terminal, width, has_usage):
    usage = {
        "known_total_tokens": 0, "usage_complete": False,
        "known_cost_usd": "0.000000", "cost_complete": False,
    } if has_usage else None
    result = AgentRunResultResponse.model_validate_json(json.dumps({
        "run_id": str(TASK_ID), "run_status": "verifying",
        "preliminary_status": "ready_for_verification", "execution_failure_code": None,
        "verification_verdict": "inconclusive", "usage": usage,
    }))

    rendered = render_terminal(result, width=width, no_color=True)

    assert f"Run ID: {TASK_ID}" in rendered
    assert "Run status: verifying" in rendered
    assert "Preliminary status: ready_for_verification" in rendered
    assert "Execution failure code: not recorded" in rendered
    assert "Verification verdict: inconclusive" in rendered
    if has_usage:
        assert "Known total tokens: 0 (incomplete)" in rendered
        assert "Known cost (USD): 0.000000 (incomplete)" in rendered
        assert "Usage complete: false" in rendered
        assert "Cost complete: false" in rendered
    else:
        assert "Known total tokens: unknown" in rendered
        assert "Known cost (USD): unknown" in rendered
        assert "Usage complete: unknown" in rendered
        assert "Cost complete: unknown" in rendered
    assert "succeeded" not in rendered


def test_run_result_plain_output_and_json_usage(capsys, monkeypatch):
    body = {
        "run_id": str(TASK_ID), "run_status": "failed",
        "preliminary_status": None, "execution_failure_code": "provider_timeout",
        "verification_verdict": None, "usage": {
            "known_total_tokens": 42, "usage_complete": True,
            "known_cost_usd": None, "cost_complete": False,
        },
    }
    result = AgentRunResultResponse.model_validate_json(json.dumps(body))

    output.print_result(result, json_output=False)

    assert capsys.readouterr() == (
        f"Run ID: {TASK_ID}\nRun status: failed\nPreliminary status: not recorded\n"
        "Execution failure code: provider_timeout\nVerification verdict: not recorded\n"
        "Known total tokens: 42\nUsage complete: true\n"
        "Known cost (USD): unknown (incomplete)\nCost complete: false\n",
        "",
    )

    def unexpected_console(*args, **kwargs):
        pytest.fail("JSON output must bypass Rich Console")

    monkeypatch.setattr(output, "Console", unexpected_console)
    output.print_result(result, json_output=True, no_color=True)
    assert capsys.readouterr() == (json.dumps(body) + "\n", "")


@pytest.mark.parametrize("presentation", ["table", "workspace", "task"])
def test_interactive_text_escapes_controls(render_terminal, presentation):
    value = "[bold]Форж[/bold] :rocket: \\ \x1b[2J\x9b31m\a\r\n\t\u202e\u2066\u2069\u200d\u2028\u2029"
    safe = r"[bold]Форж[/bold] :rocket: \\ \u001b[2J\u009b31m\u0007\r\n\t\u202e\u2066\u2069\u200d\u2028\u2029"
    workspace = WorkspaceResponse(id=WORKSPACE_ID, name=value)
    if presentation == "table":
        result = [workspace]
    elif presentation == "workspace":
        result = workspace
    else:
        result = TASK.model_copy(update={"goal": value, "acceptance_criteria": [value]})

    rendered = render_terminal(result, width=180)

    assert rendered.count(safe) == (2 if presentation == "task" else 1)
    assert all(char not in rendered for char in "\x1b\x9b\a\r\t\u202e\u2066\u2069\u200d\u2028\u2029")


def test_terminal_path_keeps_backslashes_distinct_from_controls(render_terminal):
    literal = WorkspaceResponse(id=WORKSPACE_ID, name=r"C:\forge\u001b[2J\repo")
    control = literal.model_copy(update={"name": "C:\\forge\x1b[2J\\repo"})

    assert r"Name: C:\\forge\\u001b[2J\\repo" in render_terminal(literal)
    assert r"Name: C:\\forge\u001b[2J\\repo" in render_terminal(control)


@pytest.mark.parametrize("presentation", ["table", "workspace", "task"])
def test_narrow_terminal_preserves_fields(render_terminal, presentation):
    name = "Длинное имя " * 8 + "конец"
    goal = "Длинная цель " * 10 + "конец"
    criterion = "Критерий " * 12 + "конец"
    workspace = WorkspaceResponse(id=WORKSPACE_ID, name=name)
    if presentation == "table":
        result = [workspace]
        expected = [f"ID: {WORKSPACE_ID}", f"Name: {name}"]
    elif presentation == "workspace":
        result = workspace
        expected = ["Workspace", f"ID: {WORKSPACE_ID}", f"Name: {name}"]
    else:
        result = TASK.model_copy(update={"goal": goal, "acceptance_criteria": [criterion, "Готово"]})
        expected = [
            "Task", f"ID: {TASK_ID}", f"Workspace ID: {WORKSPACE_ID}",
            f"Goal: {goal}", "Acceptance criteria:", f"1. {criterion}", "2. Готово",
        ]

    assert render_terminal(result, width=24).splitlines() == expected


def test_table_folds_long_names_without_losing_data(render_terminal):
    name = "А" * 8 + " " * 19 + "Ж" * 81
    workspace = WorkspaceResponse(id=WORKSPACE_ID, name=name)

    rendered = render_terminal([workspace], width=54)

    rows = [line for line in rendered.splitlines() if line.startswith("│")]
    assert str(WORKSPACE_ID) in rows[0]
    assert all(cell_len(row) <= 54 for row in rows)
    assert "".join(row.split("│")[2][1:-1] for row in rows).rstrip() == name


def test_task_panel_folds_long_fields_without_losing_data(render_terminal):
    goal = "цель" * 125
    criterion = "日🧪" * 150
    task = TASK.model_copy(update={"goal": goal, "acceptance_criteria": [criterion, "Готово"]})

    rendered = render_terminal(task, width=54)

    rows = [line for line in rendered.splitlines() if line.startswith("│")]
    content = [row[1:-1].strip() for row in rows]
    assert f"ID: {TASK_ID}" in content
    assert f"Workspace ID: {WORKSPACE_ID}" in content
    assert all(cell_len(row) <= 54 for row in rows)
    criteria_heading = content.index("Acceptance criteria:")
    assert "".join(content[2:criteria_heading]) == f"Goal: {goal}"
    assert "".join(content[criteria_heading + 1:-1]) == f"1. {criterion}"
    assert content[-1] == "2. Готово"


def test_empty_workspace_list_interactive_output(render_terminal):
    assert render_terminal([]) == "No workspaces found.\n"


def test_terminal_without_unicode_uses_ascii_fallback(render_terminal):
    rendered = render_terminal(WORKSPACE, encoding="ascii", no_color=False)

    assert rendered == (
        f"id={WORKSPACE_ID}\n"
        r"name=\u00e9 \u005bbold\u005d\u001b\u005b2J\r\n\t\u0000\u0085\u202e" "\n"
    )


def test_plain_output_preserves_complete_lines(monkeypatch, capsysbinary):
    monkeypatch.setenv("COLUMNS", "12")
    monkeypatch.setenv("FORCE_COLOR", "1")

    output.print_result(TASK, json_output=False)

    assert capsysbinary.readouterr() == (
        b"id=00000000-0000-0000-0000-000000000002\n"
        b"workspace_id=00000000-0000-0000-0000-000000000001\n"
        b"goal=True :rocket: a goal wider than the terminal  \n"
        b"acceptance_criteria=first\n"
        b"acceptance_criteria=second\n",
        b"",
    )


def test_plain_output_escapes_untrusted_values(capsysbinary):
    output.print_result([WORKSPACE], json_output=False)

    assert capsysbinary.readouterr() == (
        b"id=00000000-0000-0000-0000-000000000001\n"
        br"name=\u00e9 \u005bbold\u005d\u001b\u005b2J\r\n\t\u0000\u0085\u202e" + b"\n",
        b"",
    )


def test_empty_workspace_list_plain_output(capsysbinary):
    output.print_result([], json_output=False)

    assert capsysbinary.readouterr() == (b"No workspaces found.\n", b"")


def test_redirected_stdout_remains_plain_with_force_color(monkeypatch, tmp_path, capsysbinary):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TTY_INTERACTIVE", "1")
    goal = "full goal " * 49 + "last"
    criterion = "full criterion " * 19 + "last"
    task = TASK.model_copy(update={"goal": goal, "acceptance_criteria": [criterion, "second"]})
    target = tmp_path / "result.txt"

    with target.open("w", encoding="utf-8") as stream, redirect_stdout(stream):
        output.print_result(task, json_output=False)

    assert target.read_bytes() == (
        f"id={TASK_ID}\nworkspace_id={WORKSPACE_ID}\n"
        f"goal={goal}\nacceptance_criteria={criterion}\nacceptance_criteria=second\n"
    ).encode("ascii")
    assert capsysbinary.readouterr() == (b"", b"")


@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize(
    "result, expected",
    [
        (HealthResponse(status="ok"), b'{"status": "ok"}'),
        (WORKSPACE, WORKSPACE_JSON),
        ([WORKSPACE], b"[" + WORKSPACE_JSON + b"]"),
        (TASK, (
            b'{"id": "00000000-0000-0000-0000-000000000002", '
            b'"workspace_id": "00000000-0000-0000-0000-000000000001", '
            b'"goal": "True :rocket: a goal wider than the terminal  ", '
            b'"acceptance_criteria": ["first", "second"]}'
        )),
        ([], b"[]"),
    ],
    ids=["health", "workspace", "workspace-list", "task", "empty-list"],
)
def test_json_output_matches_cli03_bytes(monkeypatch, capsysbinary, result, expected, no_color):
    def unexpected_console(*args, **kwargs):
        pytest.fail("JSON output must bypass Rich Console")

    monkeypatch.setattr(output, "Console", unexpected_console)

    output.print_result(result, json_output=True, no_color=no_color)

    assert capsysbinary.readouterr() == (expected + b"\n", b"")
