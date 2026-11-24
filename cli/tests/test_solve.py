"""`forge solve` / `forge report` / `forge patch` against an in-memory fake Forge server."""

import json
import socket
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from forge_cli import commands
from forge_cli.client import ForgeAPIClient
from forge_cli.dto import WorkspaceCreate
from forge_cli.main import app
from forge_cli.solve import _criterion, sanitize

WS = "fa229b71-c6ad-4401-857f-b8204c3121e2"
TASK = "90cdd605-647e-4f27-8cbb-600eecb1578a"
RUN = "51f33760-eeb4-4f34-b56f-18456d00b84f"
runner = CliRunner()
PATCH = "--- a/calc/ops.py\n+++ b/calc/ops.py\n@@ -1 +1 @@\n-x\n+y\n"


def report_body(**override):
    body = {
        "run_id": RUN, "task_id": TASK, "run_status": "succeeded", "task_status": "succeeded",
        "goal": "fix", "agent_summary": "Fixed average.", "blocked_reason": None, "failure_code": None,
        "has_patch": True, "usage": {"steps": 5, "tool_calls": 6, "input_tokens": 12345, "output_tokens": 678},
        "verification": {
            "verdict": "pass", "reason": "tests red before, green after", "test_command": "pytest",
            "image": "python:3.12-slim", "changed_paths": ["calc/ops.py"], "protected_violations": [],
            "patched": {"exit_code": 0, "timed_out": False, "output_tail": "ok"},
            "baseline": {"exit_code": 1, "timed_out": False, "output_tail": "FAILED"},
            "duration_ms": 1234,
        },
    }
    body.update(override)
    return body


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("Tests must use MockTransport")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)


@pytest.fixture
def server(monkeypatch):
    """Fake Forge: run is already terminal at the first poll, so no test ever sleeps."""
    st = SimpleNamespace(calls=[], workspaces=[], start_status=202, run_status="succeeded",
                         report=report_body(), patch=PATCH, bodies={}, events=None)

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        st.calls.append(key)
        if request.content:
            st.bodies[key] = json.loads(request.content)
        if key == "GET /health":
            return httpx.Response(200, json={"status": "ok"})
        if key == "GET /workspaces":
            return httpx.Response(200, json=st.workspaces)
        if key == "POST /workspaces":
            ws = {"id": WS, **json.loads(request.content)}
            st.workspaces.append(ws)
            return httpx.Response(201, json=ws)
        if key == "POST /tasks":
            b = json.loads(request.content)
            return httpx.Response(201, json={"id": TASK, **b})
        if key == f"POST /tasks/{TASK}/runs":
            return httpx.Response(201, json={"id": RUN, "task_id": TASK, "attempt_number": 1,
                                             "status": "queued", "created_at": "2026-01-01T00:00:00+00:00"})
        if key == f"POST /agent-runs/{RUN}/start":
            if st.start_status != 202:
                return httpx.Response(st.start_status, json={"error": {"code": "execution_unavailable", "message": "x"}})
            return httpx.Response(202, json={"run_id": RUN, "accepted": True})
        if key == f"GET /agent-runs/{RUN}":
            return httpx.Response(200, json={"id": RUN, "task_id": TASK, "attempt_number": 1,
                                             "status": st.run_status, "created_at": "2026-01-01T00:00:00+00:00"})
        if key == f"GET /agent-runs/{RUN}/events":
            items = st.events or [
                {"sequence": 1, "created_at": "2026-01-01T00:00:00+00:00", "kind": "agent_step",
                 "details": {"step": "tool_result", "summary": "ran tests \x1b[31mRED\x1b[0m"}},
                {"sequence": 2, "created_at": "2026-01-01T00:00:01+00:00", "kind": "verification_recorded",
                 "details": {"verdict": "pass"}},
            ]
            return httpx.Response(200, json={"items": items, "next_after_sequence": items[-1]["sequence"],
                                             "has_more": False})
        if key == f"GET /agent-runs/{RUN}/report":
            return httpx.Response(200, json=st.report)
        if key == f"GET /agent-runs/{RUN}/patch":
            return httpx.Response(200, content=st.patch.encode(), headers={"content-type": "text/plain"})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": key}})

    monkeypatch.setattr(commands, "ForgeAPIClient",
                        lambda base_url: ForgeAPIClient(base_url, transport=httpx.MockTransport(handler)))
    return st


def solve_args(repo, *extra, test="python -m pytest -q"):
    return ["solve", "--repo", str(repo), "--goal", "fix average", "--test", test, *extra]


def test_verified_success_exit_zero_and_creates_configured_workspace(server, tmp_path):
    result = runner.invoke(app, solve_args(tmp_path, "--image", "python:3.12-slim"))
    assert result.exit_code == 0, result.output
    assert "SUCCEEDED" in result.output and "independent re-run" in result.output
    assert "Tests WITHOUT the change: exit 1" in result.output
    assert "Tests WITH the change:    exit 0" in result.output
    ws = server.bodies["POST /workspaces"]
    assert ws == {"name": tmp_path.name, "source_path": str(tmp_path.resolve()),
                  "test_command": "python -m pytest -q", "image": "python:3.12-slim"}
    assert server.bodies["POST /tasks"]["acceptance_criteria"] == ["`python -m pytest -q` passes"]
    assert f"forge patch {RUN}" in result.output


def test_live_steps_are_streamed_and_terminal_escapes_stripped(server, tmp_path):
    result = runner.invoke(app, solve_args(tmp_path))
    assert "ran tests" in result.output and "[verification: pass]" in result.output
    assert "\x1b" not in result.output


def test_failed_run_exits_one_and_is_labelled_not_verified(server, tmp_path):
    server.run_status = "failed"
    server.report = report_body(
        run_status="failed", task_status="failed", agent_summary=None,
        blocked_reason="I cannot fix this: the tests contradict each other.", has_patch=False,
        verification=None)
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 1
    assert "FAILED - NOT verified" in result.output
    assert "tests contradict each other" in result.output
    assert "SUCCEEDED" not in result.output
    assert "forge patch" not in result.output  # no patch to offer


def test_rejected_cheat_shows_forbidden_paths(server, tmp_path):
    server.run_status = "failed"
    v = report_body()["verification"] | {
        "verdict": "fail", "reason": "protected files modified", "changed_paths": ["tests/test_average.py"],
        "protected_violations": ["tests/test_average.py"],
        "patched": {"exit_code": 0, "timed_out": False, "output_tail": ""}}
    server.report = report_body(run_status="failed", task_status="failed", verification=v)
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 1
    assert "Forbidden changes (test files): tests/test_average.py" in result.output


def test_identical_workspace_is_reused_not_duplicated(server, tmp_path):
    server.workspaces.append({"id": WS, "name": "x", "source_path": str(tmp_path.resolve()),
                              "test_command": "python -m pytest -q", "image": None})
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 0, result.output
    assert "POST /workspaces" not in server.calls


def test_different_test_command_gets_its_own_workspace(server, tmp_path):
    server.workspaces.append({"id": "00000000-0000-4000-8000-000000000001", "name": "x",
                              "source_path": str(tmp_path.resolve()), "test_command": "other", "image": None})
    runner.invoke(app, solve_args(tmp_path))
    assert "POST /workspaces" in server.calls


def test_start_503_explains_likely_missing_model_config(server, tmp_path):
    server.start_status = 503
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 4
    assert "OPENAI_API_KEY" in result.output
    assert f"GET /agent-runs/{RUN}/events" not in server.calls


def test_no_watch_returns_right_after_start(server, tmp_path):
    result = runner.invoke(app, solve_args(tmp_path, "--no-watch"))
    assert result.exit_code == 0
    assert f"POST /agent-runs/{RUN}/start" in server.calls
    assert f"GET /agent-runs/{RUN}/report" not in server.calls


def test_missing_repo_directory_is_rejected_before_any_request(server, tmp_path):
    result = runner.invoke(app, solve_args(tmp_path / "nope"))
    assert result.exit_code == 2
    assert server.calls == []


def test_overlong_test_command_still_fits_acceptance_criterion_limit(server, tmp_path):
    long_test = "pytest " + "-k very_long_expression " * 40
    assert len(_criterion(long_test)) <= 300
    result = runner.invoke(app, solve_args(tmp_path, test=long_test))
    assert result.exit_code == 0, result.output


def test_json_mode_keeps_stdout_machine_readable(server, tmp_path):
    result = runner.invoke(app, ["--json", *solve_args(tmp_path)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_status"] == "succeeded"


def test_report_and_patch_commands(server):
    report = runner.invoke(app, ["report", RUN])
    assert report.exit_code == 0 and "SUCCEEDED" in report.output
    patch = runner.invoke(app, ["patch", RUN])
    assert patch.exit_code == 0 and patch.stdout == PATCH  # byte-exact, usable with `git apply`


def test_patch_404_is_a_clean_error(server):
    server.patch = ""
    result = runner.invoke(app, ["patch", "00000000-0000-4000-8000-0000000000ff"])
    assert result.exit_code == 4


def test_hostile_test_output_cannot_inject_terminal_escapes(server, tmp_path):
    v = report_body()["verification"]
    v["patched"] = {"exit_code": 1, "timed_out": False, "output_tail": "\x1b]0;pwned\x07boom\x1b[2J"}
    server.run_status = "failed"
    server.report = report_body(run_status="failed", task_status="failed", verification=v)
    result = runner.invoke(app, solve_args(tmp_path))
    assert "\x1b" not in result.output and "\x07" not in result.output and "boom" in result.output


def test_sanitize_keeps_newlines_and_truncates():
    assert sanitize("a\x1b[0mb\nc\td\x00") == "a[0mb\nc\td"
    assert sanitize("x" * 10, limit=4) == "xxxx…"


def test_workspace_create_body_is_unchanged_when_unconfigured():
    assert WorkspaceCreate(name="n").model_dump(mode="json") == {"name": "n"}
    assert WorkspaceCreate(name="n", test_command="t").model_dump(mode="json") == {"name": "n", "test_command": "t"}


def test_unreachable_server_gives_actionable_message_and_creates_nothing(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(commands, "ForgeAPIClient",
                        lambda base_url: ForgeAPIClient(base_url, transport=httpx.MockTransport(handler)))
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 3
    assert "scripts/start.sh" in result.output and "may have taken effect" not in result.output
    assert calls == ["/health"]  # nothing was created before the check failed


@pytest.mark.parametrize("code,expected", [
    ("provider_auth", "FORGE_MODEL_API_KEY"),
    ("provider_connection", "ollama serve"),
    ("provider_connection", "internet connection"),
    ("provider_request_rejected", "FORGE_MODEL_NAME"),
    ("provider_quota_exhausted", "billing"),
    ("sandbox_error", "Docker"),
])
def test_provider_failures_point_at_the_real_cause_not_at_the_task(server, tmp_path, code, expected):
    server.run_status = "failed"
    server.report = report_body(run_status="failed", task_status="failed", agent_summary=None,
                                failure_code=code, has_patch=False, verification=None)
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 1
    assert expected in result.output
    assert "refine the goal" not in result.output


def test_ordinary_failure_keeps_the_task_oriented_advice(server, tmp_path):
    server.run_status = "failed"
    server.report = report_body(run_status="failed", task_status="failed", blocked_reason="cannot fix",
                                has_patch=False, verification=None)
    assert "refine the goal" in runner.invoke(app, solve_args(tmp_path)).output


def test_report_shows_usage_and_says_so_when_tokens_are_not_reported(server, tmp_path):
    out = runner.invoke(app, solve_args(tmp_path)).output
    assert "Usage: 5 model calls, 6 tool calls, 12,345 input / 678 output tokens" in out
    server.report = report_body(usage={"steps": 3, "tool_calls": 2, "input_tokens": 0, "output_tokens": 0})
    assert "token usage not reported by the provider" in runner.invoke(app, solve_args(tmp_path)).output


def test_report_without_usage_field_is_still_accepted(server, tmp_path):
    body = report_body()
    body.pop("usage")                                  # e.g. a run that failed before producing an outcome
    server.report = body
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 0 and "Usage:" not in result.output


# --- rich terminal view (interactive terminals only) ----------------------------------------------------

def _step(seq, **details):
    return {"sequence": seq, "created_at": "2026-01-01T00:00:00+00:00", "kind": "agent_step", "details": details}


def _rich_events():
    return [
        _step(1, step="tool_result", summary="s", turn=1, tool="bash", ok=True, exit_code=1,
              target="python -m unittest"),
        _step(2, step="tool_result", summary="s", turn=2, tool="edit_file", ok=True, target="shopkit/shipping.py"),
        _step(3, step="tool_result", summary="s", turn=3, tool="finish", ok=True),
        _step(4, step="finish", summary="s", outcome="claimed_done"),
        {"sequence": 5, "created_at": "2026-01-01T00:00:01+00:00", "kind": "agent_run_status_changed",
         "details": {"from": "running", "to": "verifying"}},
    ]


def _tty_console(monkeypatch):
    import io

    from rich.console import Console
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=110, markup=False,
                      highlight=False)
    monkeypatch.setattr(commands, "stdout_console", lambda *, no_color: (console, True))
    return console, buf


def test_rich_view_shows_repo_steps_verification_and_diff(server, tmp_path, monkeypatch):
    server.events = _rich_events()
    console, buf = _tty_console(monkeypatch)
    result = runner.invoke(app, solve_args(tmp_path))
    assert result.exit_code == 0, result.output
    out = buf.getvalue()
    plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", out)
    assert "ForgeMini" in plain and "fix average" in plain and "python -m pytest -q" in plain
    assert "$ python -m unittest" in plain and "exit 1" in plain
    assert "edit  shopkit/shipping.py" in plain
    assert "agent claims it is done" in plain and "re-runs the tests" in plain
    assert "· finish" not in plain
    assert "Tests on the original code" in plain and "Test files untouched" in plain
    assert "-x" in plain and "+y" in plain                       # the diff is shown
    assert "VERIFIED" in plain and "NOT VERIFIED" not in plain
    assert f"forge patch {RUN} | git apply" in plain
    assert "\x1b[" in out                                        # and it is actually coloured
    assert f"GET /agent-runs/{RUN}/patch" in server.calls


def test_rich_view_says_not_verified_and_flags_touched_tests(server, tmp_path, monkeypatch):
    bad = report_body(run_status="failed", task_status="failed", has_patch=False)
    bad["verification"].update(verdict="fail", protected_violations=["tests/test_cart.py"])
    server.report = bad
    server.run_status = "failed"
    server.events = _rich_events()
    console, buf = _tty_console(monkeypatch)
    result = runner.invoke(app, solve_args(tmp_path))
    plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
    assert result.exit_code == 1
    assert "NOT VERIFIED (failed)" in plain and "tests/test_cart.py" in plain and "✗" in plain
    assert f"GET /agent-runs/{RUN}/patch" not in server.calls       # no patch -> not requested


def test_rich_view_is_off_for_json_and_for_pipes(server, tmp_path, monkeypatch):
    server.events = _rich_events()
    console, buf = _tty_console(monkeypatch)
    result = runner.invoke(app, ["--json", *solve_args(tmp_path)])
    assert json.loads(result.stdout)["run_status"] == "succeeded" and buf.getvalue() == ""
    monkeypatch.setattr(commands, "stdout_console", lambda *, no_color: (console, False))   # a pipe / CI
    plain = runner.invoke(app, solve_args(tmp_path))
    assert "RESULT: SUCCEEDED" in plain.output and "\x1b" not in plain.output   # CliRunner is not a TTY


def test_rich_view_does_not_interpret_markup_or_escapes_from_the_server(server, tmp_path, monkeypatch):
    server.events = [_step(1, step="tool_result", summary="s", tool="bash", ok=True, exit_code=0,
                           target="echo [red]HIDDEN[/red] \x1b[2J \x1b]0;pwned\x07")]
    console, buf = _tty_console(monkeypatch)
    runner.invoke(app, solve_args(tmp_path))
    out = buf.getvalue()
    assert "[red]HIDDEN[/red]" in out                              # printed literally, not applied
    assert "\x1b[2J" not in out and "\x1b]0;" not in out          # control sequences stripped


def test_solve_with_a_refused_url_exits_2_and_makes_no_request(server):
    result = runner.invoke(app, solve_args("https://user:token@github.com/o/r.git"))
    assert result.exit_code == 2 and "credentials" in result.output
    assert server.calls == []


# --- --apply ----------------------------------------------------------------------------------------------

def _git_repo(path, content="x\n"):
    import subprocess
    (path / "calc").mkdir()
    (path / "calc" / "ops.py").write_text(content)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_apply_puts_the_verified_patch_into_the_working_tree(server, tmp_path):
    _git_repo(tmp_path)
    result = runner.invoke(app, solve_args(tmp_path, "--apply"))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "calc" / "ops.py").read_text() == "y\n" and "applied to" in result.output


def test_apply_never_touches_the_tree_when_the_result_is_not_verified(server, tmp_path):
    _git_repo(tmp_path)
    server.report = report_body(run_status="failed", task_status="failed")
    server.run_status = "failed"
    result = runner.invoke(app, solve_args(tmp_path, "--apply"))
    assert result.exit_code == 1 and (tmp_path / "calc" / "ops.py").read_text() == "x\n"
    assert "applied to" not in result.output


def test_apply_conflict_exits_7_and_leaves_the_tree_alone(server, tmp_path):
    _git_repo(tmp_path, content="something else\n")
    result = runner.invoke(app, solve_args(tmp_path, "--apply"))
    assert result.exit_code == 7 and "Not applied" in result.output
    assert (tmp_path / "calc" / "ops.py").read_text() == "something else\n"


def test_without_apply_the_tree_is_never_modified(server, tmp_path):
    _git_repo(tmp_path)
    assert runner.invoke(app, solve_args(tmp_path)).exit_code == 0
    assert (tmp_path / "calc" / "ops.py").read_text() == "x\n"


def test_rich_view_prints_ids_one_per_line_and_keeps_check_rows_on_one_line(server, tmp_path, monkeypatch):
    server.events = _rich_events()
    console, buf = _tty_console(monkeypatch)
    runner.invoke(app, solve_args(tmp_path))
    plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", buf.getvalue()).splitlines()
    assert any(line.strip().startswith("workspace ") and "task " not in line for line in plain)
    assert any(line.strip().startswith("run ") for line in plain)
    original = [i for i, line in enumerate(plain) if "Tests on the original code" in line]
    assert original and "exit 1" in plain[original[0]]                     # the verdict is on the same line


def test_rich_view_tells_what_to_do_when_the_provider_failed(server, tmp_path, monkeypatch):
    server.report = report_body(run_status="failed", task_status="failed", failure_code="provider_auth",
                                has_patch=False, verification=None, agent_summary=None)
    server.run_status = "failed"
    console, buf = _tty_console(monkeypatch)
    result = runner.invoke(app, solve_args(tmp_path))
    plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
    assert result.exit_code == 1 and "NOT VERIFIED (failed)" in plain
    assert "What to do: The model provider rejected the API key" in plain and "forge run events" in plain


# --- run cost line (usage wired to the kernel collector; prices come from the server .env) -------------------------
from forge_cli.dto import UsageView  # noqa: E402
from forge_cli.solve import cost_text  # noqa: E402


def _usage(cost, status):
    return UsageView(steps=4, tool_calls=4, input_tokens=400, output_tokens=40, known_cost_usd=cost, cost_status=status)


@pytest.mark.parametrize("cost,status,expected", [
    ("0.001000", "estimated", "≈ $0.0010"),
    ("0.00000003", "estimated", "< $0.0001"),            # tiny, but never rounded down to a misleading $0.0000
    ("0.0004", "incomplete", "at least $0.0004 (some model calls could not be priced)"),
    (None, "incomplete", "unknown (the provider did not report usage for the model calls)"),
    (None, "not_priced", "not priced (set FORGE_PRICE_INPUT/OUTPUT_USD_PER_MILLION in the server .env)"),
    (None, "not_recorded", ""),
])
def test_cost_line_never_shows_unknown_as_zero(cost, status, expected):
    assert cost_text(_usage(cost, status)) == expected


def test_report_from_an_older_server_without_cost_fields_still_parses():
    usage = UsageView.model_validate({"steps": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1})
    assert (usage.known_cost_usd, usage.cost_status, cost_text(usage)) == (None, "not_recorded", "")


# --- live-demo polish (F′): labels and paths a non-engineer can read ---------------------------------------------
from forge_cli.commands import display_path  # noqa: E402
from forge_cli.solve_view import SolveView, runs_test_command  # noqa: E402

TEST_CMD = "python -m unittest tests.test_regressions.Issue1FreeShippingAtThreshold"


@pytest.mark.parametrize("command,expected", [
    (TEST_CMD, True),
    (f"ls -la; {TEST_CMD}", True),                      # what the live agent actually ran first
    (f"pip list && {TEST_CMD}", True),
    (f"{TEST_CMD} || true", False),                     # exit code is no longer the tests' verdict
    (f"{TEST_CMD} | tail -5", False),                   # pipe: the exit code is tail's
    ("python -m unittest", False),                      # a different (broader) test command
    ("", False),
])
def test_only_steps_whose_exit_code_is_the_test_commands_get_a_tests_label(command, expected):
    assert runs_test_command(command, TEST_CMD) is expected


@pytest.mark.parametrize("exit_code,label", [(1, "tests fail (exit 1)"), (0, "tests pass")])
def test_the_agents_own_test_run_is_labelled_not_shown_as_a_bare_red_exit(exit_code, label):
    step = SimpleNamespace(tool="bash", ok=True, exit_code=exit_code, target=f"ls -la; {TEST_CMD}")
    text = SolveView._tool_row(step, TEST_CMD).plain
    assert label in text and f"exit {exit_code}" not in text.replace(label, "")
    other = SolveView._tool_row(SimpleNamespace(tool="bash", ok=True, exit_code=2, target="grep x y"), TEST_CMD).plain
    assert other.endswith("exit 2") and "tests" not in other


def test_applied_path_is_shown_relative_or_home_shortened(tmp_path, monkeypatch):
    repo = tmp_path / "forge-demo-shop"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    assert display_path(repo) == "forge-demo-shop"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir("/")
    assert display_path(repo) == "~/forge-demo-shop"
    assert display_path(__import__("pathlib").Path("/opt/x")) == "/opt/x"


@pytest.mark.parametrize("cost,status,shown", [
    ("0.021300", "estimated", "≈ $0.0213"),
    (None, "not_priced", "cost not configured"),
    (None, "not_recorded", None),
])
def test_verified_panel_shows_the_cost_or_says_why_not(cost, status, shown):
    from rich.console import Console
    report = SimpleNamespace(usage=_usage(cost, status), has_patch=False, run_status="succeeded")
    console = Console(record=True, width=160, color_system=None)
    console.print(SolveView(console)._result_panel(report, RUN, True))
    text = console.export_text()
    assert "VERIFIED" in text
    assert (shown in text) if shown else ("cost" not in text.lower() and "$" not in text)


# --- F′ (2): the verdict card speaks in tests, not exit codes -------------------------------------------------------
from forge_cli.solve import count_tests, describe_test_run  # noqa: E402


@pytest.mark.parametrize("tail,expected", [
    ("FAIL: test_a\n----\nRan 2 tests in 0.001s\n\nFAILED (failures=2)\n", (2, 2)),
    ("Ran 5 tests in 0.010s\n\nFAILED (failures=1, errors=2)\n", (3, 5)),
    ("..\n----\nRan 2 tests in 0.000s\n\nOK\n", (0, 2)),
    ("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n", (0, 0)),
    ("==== 2 failed, 3 passed in 0.12s ====", (2, 5)),
    ("==== 1 failed, 1 error in 0.50s ====", (2, 2)),
    ("======= 4 passed in 1.02s =======", (0, 4)),
    ("FAILED", None),                                          # no summary we recognise: no invented number
    ("", None),
])
def test_counts_come_only_from_the_runners_own_summary(tail, expected):
    assert count_tests(tail) == expected


@pytest.mark.parametrize("tail,exit_code,timed_out,line", [
    ("Ran 2 tests in 0.0s\n\nFAILED (failures=2)", 1, False, "2 of 2 failed (exit 1)"),
    ("Ran 2 tests in 0.0s\n\nOK", 0, False, "all 2 passed (exit 0)"),
    ("Ran 0 tests in 0.0s\n\nNO TESTS RAN", 5, False, "no tests ran (exit 5)"),
    ("killed", 137, True, "exit 137 (timed out)"),
])
def test_describe_test_run(tail, exit_code, timed_out, line):
    assert describe_test_run(SimpleNamespace(output_tail=tail, exit_code=exit_code, timed_out=timed_out)) == line


# --- the refuse demo printed the agent's reason twice ("Agent says" + "Why it stopped") ----------------------------
from forge_cli.solve import stop_reason_text  # noqa: E402

WORDS = "The requested threshold of 40.00 contradicts the uneditable shipping test."


@pytest.mark.parametrize("summary,reason,shown", [
    (WORDS, f"agent_cannot_fix: {WORDS}", "agent_cannot_fix (the agent says it cannot do this; its reasons are above)"),
    (WORDS, WORDS, None),                                                       # identical: say it once
    ("Fixed it", "agent_reported_done_but_changed_nothing", "agent_reported_done_but_changed_nothing"),
    ("gave up", "step_budget_exhausted after 25 steps", "step_budget_exhausted after 25 steps"),
    (None, "agent_cannot_fix: something", "agent_cannot_fix: something"),       # no summary: keep everything
    ("x", None, None),
])
def test_why_it_stopped_never_repeats_what_the_agent_said(summary, reason, shown):
    assert stop_reason_text(SimpleNamespace(agent_summary=summary, blocked_reason=reason)) == shown


# --- --setup (ADR-004) ---------------------------------------------------------------------------------------------
def test_setup_command_is_stored_in_the_workspace(server, tmp_path):
    result = runner.invoke(app, solve_args(tmp_path, "--setup", "pip install -r requirements.txt"))
    assert result.exit_code == 0, result.output
    assert server.bodies["POST /workspaces"]["setup_command"] == "pip install -r requirements.txt"


def test_without_setup_the_request_body_is_unchanged(server, tmp_path):
    runner.invoke(app, solve_args(tmp_path))
    assert "setup_command" not in server.bodies["POST /workspaces"]          # old servers keep working


def test_workspace_with_a_different_setup_is_not_reused(server, tmp_path):
    # Reusing it would silently run the agent WITHOUT the dependencies the user asked for (or with stale ones).
    server.workspaces.append({"id": WS, "name": "x", "source_path": str(tmp_path.resolve()),
                              "test_command": "python -m pytest -q", "image": None, "setup_command": None})
    runner.invoke(app, solve_args(tmp_path, "--setup", "pip install ."))
    assert "POST /workspaces" in server.calls
    server.calls.clear()
    runner.invoke(app, solve_args(tmp_path, "--setup", "pip install ."))
    assert "POST /workspaces" not in server.calls                            # identical setup: reused


def test_rich_view_shows_the_setup_phase_in_the_banner_and_as_steps(server, tmp_path, monkeypatch):
    server.events = [_step(1, step="setup", summary="setup: installing dependencies (with network)… $ pip install ."),
                     _step(2, step="setup", summary="setup: dependencies installed $ pip install ."),
                     *[{**e, "sequence": e["sequence"] + 2} for e in _rich_events()]]
    console, buf = _tty_console(monkeypatch)
    result = runner.invoke(app, solve_args(tmp_path, "--setup", "pip install ."))
    assert result.exit_code == 0, result.output
    plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
    assert "setup" in plain and "(with network, before the sandbox)" in plain       # banner
    assert "⚙ setup: installing dependencies" in plain and "⚙ setup: dependencies installed" in plain
