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
        "has_patch": True,
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
                         report=report_body(), patch=PATCH, bodies={})

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        st.calls.append(key)
        if request.content:
            st.bodies[key] = json.loads(request.content)
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
            return httpx.Response(200, json={"items": [
                {"sequence": 1, "created_at": "2026-01-01T00:00:00+00:00", "kind": "agent_step",
                 "details": {"step": "tool_result", "summary": "ran tests \x1b[31mRED\x1b[0m"}},
                {"sequence": 2, "created_at": "2026-01-01T00:00:01+00:00", "kind": "verification_recorded",
                 "details": {"verdict": "pass"}},
            ], "next_after_sequence": 2, "has_more": False})
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
    assert "Tests WITHOUT the change: exit code 1" in result.output
    assert "Tests WITH the change:    exit code 0" in result.output
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
