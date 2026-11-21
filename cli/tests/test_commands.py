import json
import socket
import sys
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from forge_cli import commands
from forge_cli.client import ForgeAPIClient
from forge_cli.main import app

WORKSPACE_ID = UUID("fa229b71-c6ad-4401-857f-b8204c3121e2")
TASK_ID = UUID("90cdd605-647e-4f27-8cbb-600eecb1578a")
RUN_ID = UUID("51f33760-eeb4-4f34-b56f-18456d00b84f")
KEY = UUID("7ec8f5ab-5459-43ba-8816-743f6595ae10")
SECRET = "sensitive-command-marker"
TASK_ARGS = [
    "task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", "goal",
    "--criterion", "first", "--criterion", "second",
]
RUN_CREATE_ARGS = ["run", "create", "--task-id", str(TASK_ID)]
RUN_GET_ARGS = ["run", "get", str(RUN_ID)]
runner = CliRunner()


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests must use MockTransport")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(
        requests=[], clients=[], base_urls=[], failure=None,
        response=httpx.Response(200, json={"status": "ok"}),
    )

    def handler(request):
        state.requests.append(request)
        if state.failure is not None:
            raise state.failure(SECRET + " " + str(request.url), request=request)
        return state.response

    def factory(base_url):
        state.base_urls.append(base_url)
        client = ForgeAPIClient(base_url, transport=httpx.MockTransport(handler))
        state.clients.append(client)
        return client

    monkeypatch.setattr(commands, "ForgeAPIClient", factory)
    return state


@pytest.fixture
def task_body():
    return {
        "id": str(TASK_ID), "workspace_id": str(WORKSPACE_ID),
        "goal": "goal", "acceptance_criteria": ["first", "second"],
    }


@pytest.mark.parametrize("args", [
    ["--help"], ["health", "--help"], ["workspace", "--help"],
    ["workspace", "create", "--help"], ["workspace", "list", "--help"],
    ["task", "--help"], ["task", "create", "--help"], ["task", "get", "--help"],
    ["run", "--help"], ["run", "create", "--help"], ["run", "get", "--help"],
    ["run", "start", "--help"], ["run", "cancel", "--help"],
    ["run", "events", "--help"], ["run", "result", "--help"],
    ["--version"], [], ["--no-color", "--help"],
    ["--no-color", "task", "get", "--help"], ["--no-color", "--version"],
])
def test_help_and_version_need_no_client(api, args):
    result = runner.invoke(app, args, env={"FORCE_COLOR": "1"}, color=True)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    expected = "0.1.0" if "--version" in args else "Usage:"
    assert expected in result.stdout
    assert "\x1b" not in result.stdout
    if args in (["--help"], ["--no-color", "--help"]):
        assert "--no-color" in result.stdout
    assert api.clients == []
    assert api.requests == []


@pytest.mark.parametrize("json_mode", [False, True])
def test_health_command(api, json_mode):
    result = runner.invoke(app, (["--json"] if json_mode else []) + ["health"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if json_mode:
        assert json.loads(result.stdout) == {"status": "ok"}
    else:
        assert result.stdout == "status=ok\n"
    assert len(api.requests) == 1
    assert api.requests[0].url.path == "/health"
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
def test_workspace_create_command(api, json_mode):
    body = {"id": str(WORKSPACE_ID), "name": "Forge"}
    api.response = httpx.Response(201, json=body)
    args = (["--json"] if json_mode else []) + ["workspace", "create", "--name", " Forge "]

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/workspaces"
    assert json.loads(request.content) == {"name": "Forge"}
    if json_mode:
        assert json.loads(result.stdout) == body
    else:
        assert str(WORKSPACE_ID) in result.stdout
        assert "name=Forge\n" in result.stdout
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_workspace_list_command(api, json_mode, empty):
    body = [] if empty else [{"id": str(WORKSPACE_ID), "name": "Forge"}]
    api.response = httpx.Response(200, json=body)

    result = runner.invoke(app, (["--json"] if json_mode else []) + ["workspace", "list"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert len(api.requests) == 1
    assert api.requests[0].method == "GET"
    assert api.requests[0].url.path == "/workspaces"
    if json_mode:
        assert json.loads(result.stdout) == body
    elif empty:
        assert result.stdout == "No workspaces found.\n"
    else:
        assert str(WORKSPACE_ID) in result.stdout
        assert "name=Forge\n" in result.stdout
    assert api.clients[0].is_closed


def test_global_no_color_preserves_workspace_table(api, monkeypatch):
    api.response = httpx.Response(200, json=[{"id": str(WORKSPACE_ID), "name": "Проект"}])
    create_client = commands.ForgeAPIClient

    def create_client_with_tty(base_url):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        return create_client(base_url)

    monkeypatch.setattr(commands, "ForgeAPIClient", create_client_with_tty)
    for variable in ("NO_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(variable, raising=False)
    env = {"FORCE_COLOR": "1", "TERM": "xterm-256color", "COLUMNS": "80"}

    colored = runner.invoke(app, ["workspace", "list"], env=env, color=True)
    plain = runner.invoke(app, ["--no-color", "workspace", "list"], env=env, color=True)

    assert colored.exit_code == plain.exit_code == 0
    assert colored.stderr == plain.stderr == ""
    assert "\x1b[" in colored.stdout
    assert "\x1b" not in plain.stdout
    assert "┏" in colored.stdout and "┏" in plain.stdout
    assert "Проект" in plain.stdout and str(WORKSPACE_ID) in plain.stdout


def test_global_no_color_with_json(api):
    api.response = httpx.Response(200, json=[{"id": str(WORKSPACE_ID), "name": "Проект"}])

    result = runner.invoke(
        app, ["--no-color", "--json", "workspace", "list"], env={"FORCE_COLOR": "1"}, color=True
    )

    escaped_name = r"\u041f\u0440\u043e\u0435\u043a\u0442"
    expected = f'[{{"id": "{WORKSPACE_ID}", "name": "{escaped_name}"}}]\n'
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == expected.encode("ascii")
    assert result.stderr_bytes == b""


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("key", [KEY, None])
def test_task_create_command(api, task_body, json_mode, key):
    api.response = httpx.Response(201, json=task_body)
    args = (["--json"] if json_mode else []) + TASK_ARGS
    if key is not None:
        args += ["--idempotency-key", str(key)]

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/tasks"
    assert json.loads(request.content) == {
        "workspace_id": str(WORKSPACE_ID), "goal": "goal", "acceptance_criteria": ["first", "second"],
    }
    assert request.headers.get("Idempotency-Key") == (str(key) if key else None)
    if key:
        assert request.headers.raw.count((b"Idempotency-Key", str(key).encode())) == 1
    if json_mode:
        assert json.loads(result.stdout) == task_body
    else:
        assert str(TASK_ID) in result.stdout
        assert str(WORKSPACE_ID) in result.stdout
        assert "acceptance_criteria=first\nacceptance_criteria=second\n" in result.stdout
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
def test_task_get_command(api, task_body, json_mode):
    api.response = httpx.Response(200, json=task_body)

    result = runner.invoke(app, (["--json"] if json_mode else []) + ["task", "get", str(TASK_ID)])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert len(api.requests) == 1
    assert api.requests[0].method == "GET"
    assert api.requests[0].url.path == f"/tasks/{TASK_ID}"
    if json_mode:
        assert json.loads(result.stdout) == task_body
    else:
        assert str(TASK_ID) in result.stdout
        assert "goal=goal\n" in result.stdout
    assert api.clients[0].is_closed


@pytest.mark.parametrize(
    "args, method, path, status, run_status",
    [
        (RUN_CREATE_ARGS, "POST", f"/tasks/{TASK_ID}/runs", 201, "queued"),
        (RUN_CREATE_ARGS, "POST", f"/tasks/{TASK_ID}/runs", 200, "queued"),
        (RUN_GET_ARGS, "GET", f"/agent-runs/{RUN_ID}", 200, "running"),
    ],
)
@pytest.mark.parametrize("json_mode", [False, True])
def test_run_commands(api, args, method, path, status, run_status, json_mode):
    body = {
        "id": str(RUN_ID),
        "task_id": str(TASK_ID),
        "attempt_number": 1,
        "status": run_status,
        "created_at": "2026-11-21T09:30:00Z",
    }
    api.response = httpx.Response(status, json=body)
    options = ["--no-color", "--json"] if json_mode else []

    result = runner.invoke(app, options + args)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert [(request.method, request.url.path) for request in api.requests] == [(method, path)]
    assert api.requests[0].content == b""
    assert api.clients[0].is_closed
    if json_mode:
        assert json.loads(result.stdout) == body
    else:
        assert result.stdout.splitlines() == [f"{key}={value}" for key, value in body.items()]


@pytest.mark.parametrize("args", [
    ["run", "create"],
    ["run", "create", "--task-id", "invalid"],
    ["run", "get", "invalid"],
    ["run", "start", "invalid"],
    ["run", "cancel", "invalid"],
    ["run", "events", "invalid"],
    ["run", "result", "invalid"],
    ["run", "events", str(RUN_ID), "--after-sequence", "-1"],
    ["run", "events", str(RUN_ID), "--after-sequence", "2147483648"],
    ["run", "events", str(RUN_ID), "--after-sequence", "1.5"],
    ["run", "events", str(RUN_ID), "--limit", "0"],
    ["run", "events", str(RUN_ID), "--limit", "101"],
    ["run", "events", str(RUN_ID), "--limit", "true"],
])
def test_run_invalid_input_sends_no_http(api, args):
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr
    assert api.clients == []
    assert api.requests == []


@pytest.mark.parametrize(
    "args, status, code",
    [
        (RUN_CREATE_ARGS, 404, "TASK_NOT_FOUND"),
        (RUN_CREATE_ARGS, 409, "AGENT_RUN_CREATE_CONFLICT"),
        (RUN_CREATE_ARGS, 422, "VALIDATION_ERROR"),
        (RUN_GET_ARGS, 404, "AGENT_RUN_NOT_FOUND"),
        (RUN_GET_ARGS, 422, "VALIDATION_ERROR"),
    ],
)
def test_run_api_errors_are_redacted(api, args, status, code):
    api.response = httpx.Response(status, json={
        "error": {"code": code, "message": SECRET, "details": [{"field": "path", "message": SECRET}]}
    })

    result = runner.invoke(app, ["--json"] + args)

    assert result.exit_code == 4
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert code not in result.stderr
    assert SECRET not in result.stderr
    assert "http://" not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


def test_run_malformed_response_is_redacted(api):
    api.response = httpx.Response(201, text=SECRET)

    result = runner.invoke(app, RUN_CREATE_ARGS)

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "invalid response" in result.stderr
    assert SECRET not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize(
    "args, http_status, changes",
    [
        (RUN_GET_ARGS, 200, {"id": str(WORKSPACE_ID)}),
        (RUN_CREATE_ARGS, 200, {"task_id": str(WORKSPACE_ID)}),
        (RUN_CREATE_ARGS, 201, {"status": "running"}),
    ],
    ids=["get-wrong-run", "create-200-wrong-task", "create-201-not-queued"],
)
def test_run_inconsistent_response_exits_5(api, args, http_status, changes):
    api.response = httpx.Response(http_status, json={
        "id": str(RUN_ID),
        "task_id": str(TASK_ID),
        "attempt_number": 1,
        "status": "queued",
        "created_at": "2026-11-21T09:30:00Z",
        "note": SECRET,
        **changes,
    })

    result = runner.invoke(app, ["--json", "--base-url", "https://forge.example.invalid"] + args)

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "invalid response" in result.stderr
    assert f"HTTP {http_status}" in result.stderr
    for private_value in (SECRET, "forge.example.invalid", str(RUN_ID), str(TASK_ID), str(WORKSPACE_ID)):
        assert private_value not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


def test_run_creation_timeout_is_not_retried(api):
    api.failure = httpx.ReadTimeout

    result = runner.invoke(app, RUN_CREATE_ARGS)

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "timeout" in result.stderr
    assert SECRET not in result.stderr
    assert [(request.method, request.url.path) for request in api.requests] == [
        ("POST", f"/tasks/{TASK_ID}/runs")
    ]
    assert api.clients[0].is_closed


@pytest.mark.parametrize("action, status", [("start", 202), ("cancel", 200)])
@pytest.mark.parametrize("json_mode", [False, True])
def test_start_and_cancel_commands(api, action, status, json_mode):
    if action == "start":
        body = {"run_id": str(RUN_ID), "accepted": True}
        expected_text = f"run_id={RUN_ID}\naccepted=true\n"
    else:
        body = {
            "id": str(RUN_ID), "task_id": str(TASK_ID), "attempt_number": 2,
            "status": "cancelled", "created_at": "2026-11-21T09:30:00Z",
        }
        expected_text = "".join(f"{key}={value}\n" for key, value in body.items())
    api.response = httpx.Response(
        status, json=body, headers={"Location": f"https://other.example.invalid/{SECRET}"}
    )
    options = ["--no-color", "--json"] if json_mode else []

    result = runner.invoke(app, options + ["run", action, str(RUN_ID)])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert [(request.method, request.url.path, request.content) for request in api.requests] == [
        ("POST", f"/agent-runs/{RUN_ID}/{action}", b"")
    ]
    assert api.clients[0].is_closed
    if json_mode:
        decoded = json.loads(result.stdout)
        assert decoded == body
        if action == "start":
            assert decoded["accepted"] is True
    else:
        assert result.stdout == expected_text


@pytest.mark.parametrize(
    "action, status, code",
    [
        ("start", 404, "AGENT_RUN_NOT_FOUND"),
        ("start", 409, "RUN_ALREADY_STARTED"),
        ("start", 422, "VALIDATION_ERROR"),
        ("start", 429, "RUN_CAPACITY_EXCEEDED"),
        ("start", 503, "RUN_EXECUTION_UNAVAILABLE"),
        ("cancel", 404, "AGENT_RUN_NOT_FOUND"),
        ("cancel", 409, "RUN_NOT_CANCELLABLE"),
        ("cancel", 422, "VALIDATION_ERROR"),
        ("cancel", 503, "RUN_EXECUTION_UNAVAILABLE"),
    ],
)
def test_start_and_cancel_api_errors_are_redacted(api, action, status, code):
    api.response = httpx.Response(
        status,
        json={"error": {
            "code": code, "message": SECRET, "details": [{"field": "run_id", "message": SECRET}],
        }},
        headers={"Location": f"https://other.example.invalid/{SECRET}"},
    )

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert f"/agent-runs/{{run_id}}/{action}" in result.stderr
    for private_value in (code, SECRET, "other.example.invalid", str(RUN_ID), "Traceback"):
        assert private_value not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize("action", ["start", "cancel"])
def test_start_and_cancel_invalid_responses_exit_5(api, action):
    if action == "start":
        status = 202
        body = {"run_id": str(TASK_ID), "accepted": True}
    else:
        status = 200
        body = {
            "id": str(RUN_ID), "task_id": str(TASK_ID), "attempt_number": 1,
            "status": "queued", "created_at": "2026-11-21T09:30:00Z",
        }
    api.response = httpx.Response(status, json={**body, "note": SECRET})

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert f"/agent-runs/{{run_id}}/{action}" in result.stderr
    for private_value in (SECRET, str(RUN_ID), str(TASK_ID), "Traceback"):
        assert private_value not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize("action", ["start", "cancel"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_start_and_cancel_transport_failures_advise_status_check(api, action, failure):
    api.failure = failure

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "may have taken effect" in result.stderr
    assert "forge run get UUID" in result.stderr
    for private_value in (SECRET, "127.0.0.1", str(RUN_ID), "Traceback"):
        assert private_value not in result.stderr
    assert [(request.method, request.url.path) for request in api.requests] == [
        ("POST", f"/agent-runs/{RUN_ID}/{action}")
    ]
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
def test_run_events_command_returns_one_page(api, json_mode):
    page = {
        "items": [{
            "sequence": 16, "created_at": "2026-11-21T09:30:00Z",
            "kind": "agent_run_status_changed", "details": {"from": "queued", "to": "running"},
        }],
        "next_after_sequence": 16, "has_more": True,
    }
    api.response = httpx.Response(200, json=page)
    options = ["--no-color", "--json"] if json_mode else ["--no-color"]

    result = runner.invoke(
        app, options + ["run", "events", str(RUN_ID), "--after-sequence", "10", "--limit", "1"]
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/agent-runs/{RUN_ID}/events"
    assert dict(request.url.params) == {"after_sequence": "10", "limit": "1"}
    assert api.clients[0].is_closed
    if json_mode:
        assert json.loads(result.stdout) == page
        assert json.loads(result.stdout)["has_more"] is True
    else:
        assert result.stdout == (
            "#16  2026-11-21T09:30:00+00:00\nKind: agent_run_status_changed\n"
            "From: queued\nTo: running\n\nNext after sequence: 16\nHas more: true\n"
        )


@pytest.mark.parametrize(
    "status, preliminary, failure, verdict",
    [
        ("queued", None, None, None),
        ("failed", None, "provider_timeout", None),
        ("cancelled", None, None, None),
        ("verifying", "ready_for_verification", None, "inconclusive"),
        ("succeeded", "ready_for_verification", None, "pass"),
    ],
)
def test_run_result_command_reads_snapshot_without_inventing_success(api, status, preliminary, failure, verdict):
    body = {
        "run_id": str(RUN_ID), "run_status": status, "preliminary_status": preliminary,
        "execution_failure_code": failure, "verification_verdict": verdict, "usage": None,
    }
    api.response = httpx.Response(200, json=body)

    result = runner.invoke(app, ["--no-color", "--json", "run", "result", str(RUN_ID)])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == body
    assert [(req.method, req.url.path) for req in api.requests] == [
        ("GET", f"/agent-runs/{RUN_ID}/result")
    ]
    assert api.clients[0].is_closed


@pytest.mark.parametrize("action", ["events", "result"])
@pytest.mark.parametrize("status", [404, 422, 500])
def test_run_read_api_errors_are_redacted(api, action, status):
    api.response = httpx.Response(
        status, json={"error": {"code": SECRET, "message": SECRET, "details": []}}
    )

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert f"/agent-runs/{{run_id}}/{action}" in result.stderr
    assert SECRET not in result.stderr
    assert str(RUN_ID) not in result.stderr
    assert len(api.requests) == 1


@pytest.mark.parametrize("action", ["events", "result"])
def test_run_read_transport_failure_is_redacted(api, action):
    api.failure = httpx.ReadTimeout

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "timeout" in result.stderr
    assert "may have taken effect" not in result.stderr
    assert SECRET not in result.stderr
    assert "127.0.0.1" not in result.stderr
    assert [(req.method, req.url.path) for req in api.requests] == [
        ("GET", f"/agent-runs/{RUN_ID}/{action}")
    ]
    assert api.clients[0].is_closed


@pytest.mark.parametrize("action", ["events", "result"])
def test_run_read_invalid_response_exits_5(api, action):
    if action == "events":
        body = {
            "items": [{"sequence": 1, "kind": "other", "created_at": "2026-11-21T09:30:00Z",
                       "details": {"payload": SECRET}}],
            "next_after_sequence": 1, "has_more": False,
        }
    else:
        body = {
            "run_id": str(RUN_ID), "run_status": "succeeded", "preliminary_status": None,
            "execution_failure_code": None, "verification_verdict": None, "usage": None,
        }
    api.response = httpx.Response(200, json=body)

    result = runner.invoke(app, ["--json", "run", action, str(RUN_ID)])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "invalid response" in result.stderr
    assert f"/agent-runs/{{run_id}}/{action}" in result.stderr
    assert SECRET not in result.stderr
    assert str(RUN_ID) not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1


@pytest.mark.parametrize("flag", [None, "https://flag.example.invalid/prefix"])
def test_global_configuration_reaches_commands(api, monkeypatch, flag):
    monkeypatch.setenv("FORGE_BASE_URL", "https://environment.example.invalid/prefix")
    args = ["--json"]
    if flag is not None:
        args += ["--base-url", flag]

    result = runner.invoke(app, args + ["health"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": "ok"}
    assert api.base_urls == [flag or "https://environment.example.invalid/prefix"]
    assert api.requests[0].url.path == "/prefix/health"


@pytest.mark.parametrize("args", [
    ["workspace", "create"],
    ["workspace", "create", "--name", " \t"],
    ["workspace", "create", "--name", "n" * 101],
    ["task", "get", "invalid"],
    ["task", "create", "--workspace-id", "invalid", "--goal", "goal", "--criterion", "one"],
    ["task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", "goal"],
    TASK_ARGS + ["--idempotency-key", "invalid"],
    ["task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", " ", "--criterion", "one"],
    ["task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", "g" * 501, "--criterion", "one"],
    ["task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", "goal", "--criterion", " "],
    ["task", "create", "--workspace-id", str(WORKSPACE_ID), "--goal", "goal", "--criterion", "c" * 301],
    TASK_ARGS + ["--criterion", "extra"] * 9,
])
def test_invalid_input_makes_no_http_request(api, args):
    result = runner.invoke(app, args)

    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr
    assert api.requests == []
    assert all(client.is_closed for client in api.clients)


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
def test_transport_errors_exit_3(api, json_mode, failure):
    api.failure = failure

    result = runner.invoke(app, (["--json"] if json_mode else []) + ["health"])

    assert result.exit_code == 3, result.output
    assert result.stdout == ""
    assert "transport failure" in result.stderr
    assert SECRET not in result.stderr
    assert "127.0.0.1" not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("status,args", [(404, ["task", "get", str(TASK_ID)]), (409, TASK_ARGS), (422, TASK_ARGS), (200, TASK_ARGS)])
def test_api_errors_exit_4(api, json_mode, status, args):
    api.response = httpx.Response(status, json={"error": {
        "code": SECRET, "message": SECRET, "details": [{"field": SECRET, "message": SECRET}],
    }})

    result = runner.invoke(app, (["--json"] if json_mode else []) + args)

    assert result.exit_code == 4, result.output
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert SECRET not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"status":"unexpected"}'])
def test_response_errors_exit_5(api, json_mode, body):
    api.response = httpx.Response(200, content=body)

    result = runner.invoke(app, (["--json"] if json_mode else []) + ["health"])

    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    assert "invalid response" in result.stderr
    assert "Traceback" not in result.stderr
    assert len(api.requests) == 1
    assert api.clients[0].is_closed


@pytest.mark.parametrize("command", ["workspace", "task"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_untrusted_values_are_escaped(api, task_body, command, json_mode):
    value = "[bold]unsafe[/bold]\x1b[2J\n\r\t\x00\x07\x85\u202e"
    if command == "workspace":
        body = [{"id": str(WORKSPACE_ID), "name": value}]
        args = ["workspace", "list"]
    else:
        task_body.update(goal=value, acceptance_criteria=[value])
        body = task_body
        args = ["task", "get", str(TASK_ID)]
    api.response = httpx.Response(200, json=body)

    result = runner.invoke(app, (["--json"] if json_mode else []) + args)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert all(ord(char) >= 32 or char == "\n" for char in result.stdout)
    assert "\x85" not in result.stdout
    assert "\u202e" not in result.stdout
    if json_mode:
        assert json.loads(result.stdout) == body
    else:
        assert "[bold]" not in result.stdout
        assert "\\u005bbold\\u005d" in result.stdout
        assert "\\u001b" in result.stdout


def test_uuid_diagnostic_does_not_echo_untrusted_input(api):
    result = runner.invoke(app, ["task", "get", SECRET + "\x1b[2J"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Expected a UUID" in result.stderr
    assert SECRET not in result.stderr
    assert "\x1b" not in result.stderr
    assert api.requests == []


@pytest.mark.parametrize("no_color", [False, True])
def test_error_fields_cannot_inject_terminal_output(api, no_color):
    value = SECRET + "[bold]\x1b[2J\n\u202e"
    api.response = httpx.Response(500, json={"error": {
        "code": value, "message": value, "details": [{"field": value, "message": value}],
    }})

    args = ["--no-color"] if no_color else []
    args += ["--base-url", "https://forge.example.invalid/" + SECRET, "health"]
    result = runner.invoke(app, args, env={"FORCE_COLOR": "1"}, color=True)

    assert result.exit_code == 4
    assert result.stdout == ""
    assert SECRET not in result.stderr
    assert "[bold]" not in result.stderr
    assert "\x1b" not in result.stderr
    assert "\u202e" not in result.stderr
    assert "forge.example.invalid" not in result.stderr


def test_post_transport_failure_is_not_retried(api):
    api.failure = httpx.ReadTimeout

    result = runner.invoke(app, TASK_ARGS)

    assert result.exit_code == 3
    assert result.stdout == ""
    assert len(api.requests) == 1
    assert api.requests[0].method == "POST"
    assert api.clients[0].is_closed
