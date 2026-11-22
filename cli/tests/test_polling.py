import json
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from forge_cli import commands, output
from forge_cli.client import ForgeAPIClient
from forge_cli.main import app
from forge_cli.polling import WatchOutcome, watch_run

RUN_ID = UUID(int=1)
TASK_ID = UUID(int=2)
BASE_URL = "https://forge.example.invalid"
MARKER = "private-watch-input"
CREATED_AT = "2026-11-21T09:30:00Z"
runner = CliRunner()


@dataclass
class FakeClock:
    now: float = 0
    sleeps: list[float] = field(default_factory=list)
    interrupt_sleep: bool = False

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.interrupt_sleep:
            raise KeyboardInterrupt
        self.now += seconds


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Polling tests must use MockTransport")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)


@dataclass
class ScriptedHTTP:
    replies: list[httpx.Response | Callable[[], httpx.Response]]
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[ForgeAPIClient] = field(default_factory=list)

    def send(self, request):
        assert request.method == "GET"
        self.requests.append(request)
        assert self.replies, "Unexpected request after scripted replies"
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply

    def open(self, base_url):
        client = ForgeAPIClient(base_url, transport=httpx.MockTransport(self.send))
        self.clients.append(client)
        return client


def metadata(status):
    return httpx.Response(200, json={
        "id": str(RUN_ID), "task_id": str(TASK_ID), "attempt_number": 1,
        "status": status, "created_at": CREATED_AT,
    })


def event(sequence):
    return {
        "kind": "agent_run_status_changed", "sequence": sequence,
        "created_at": CREATED_AT, "details": {"from": "queued", "to": "running"},
    }


def page(*sequences, cursor=0, more=False):
    return httpx.Response(200, json={
        "items": [event(sequence) for sequence in sequences],
        "next_after_sequence": sequences[-1] if sequences else cursor,
        "has_more": more,
    })


def check_requests(http, cursors):
    expected = []
    for cursor in cursors:
        expected.append((f"/agent-runs/{RUN_ID}", {}))
        expected.append((f"/agent-runs/{RUN_ID}/events", {
            "after_sequence": str(cursor), "limit": "50",
        }))
    assert [(req.url.path, dict(req.url.params)) for req in http.requests] == expected
    assert len(http.clients) == 1
    assert http.clients[0].is_closed


def observe(http, clock, *, interval=2, timeout=10, after_sequence=0, pause=None):
    observations = []
    with http.open(BASE_URL) as client:
        outcome = watch_run(
            client, RUN_ID, interval=interval, timeout=timeout, after_sequence=after_sequence,
            clock=clock.monotonic, pause=clock.sleep if pause is None else pause,
            on_page=lambda status, batch: observations.append(
                (status, [item.sequence for item in batch.items])
            ),
        )
    return outcome, observations


def test_watch_observes_lifecycle_and_advances_cursor():
    http = ScriptedHTTP([
        metadata("queued"), page(cursor=1),
        metadata("running"), page(2, 5),
        metadata("verifying"), page(cursor=5),
        metadata("succeeded"), page(8),
    ])
    clock = FakeClock()

    outcome, observations = observe(http, clock, after_sequence=1)

    assert outcome == WatchOutcome("succeeded", 8, "terminal")
    assert observations == [
        ("queued", []), ("running", [2, 5]), ("verifying", []), ("succeeded", [8]),
    ]
    assert clock.sleeps == [2, 2, 2]
    check_requests(http, [1, 1, 5, 5])


@pytest.mark.parametrize(
    "status, next_status, reason",
    [
        ("succeeded", "succeeded", "terminal"),
        ("failed", "failed", "terminal"),
        ("cancelled", "cancelled", "terminal"),
    ],
)
def test_watch_drains_stop_state_across_cycles(status, next_status, reason):
    first = list(range(2, 101, 2))
    http = ScriptedHTTP([
        metadata(status), page(*first, more=True), metadata(next_status), page(103),
    ])
    clock = FakeClock()

    outcome, observations = observe(http, clock)

    assert outcome == WatchOutcome(next_status, 103, reason)
    assert observations == [(status, first), (next_status, [103])]
    assert clock.sleeps == [2]
    check_requests(http, [0, 100])


def test_watch_caps_sleep_and_stops_before_next_request():
    http = ScriptedHTTP([metadata("running"), page(), metadata("running"), page()])
    clock = FakeClock()

    outcome, observations = observe(http, clock, timeout=3)

    assert outcome == WatchOutcome("running", 0, "timeout")
    assert observations == [("running", []), ("running", [])]
    assert clock.sleeps == [2, 1]
    check_requests(http, [0, 0])


@pytest.mark.parametrize("phase", ["metadata", "events"])
def test_watch_overrun_does_not_issue_another_request(phase):
    clock = FakeClock()

    def overrun(response):
        clock.now += 1.5
        return response

    if phase == "metadata":
        http = ScriptedHTTP([lambda: overrun(metadata("running"))])
    else:
        http = ScriptedHTTP([metadata("running"), lambda: overrun(page(6))])

    outcome, observations = observe(http, clock, timeout=1, after_sequence=5)

    assert outcome == WatchOutcome("running", 5, "timeout")
    assert observations == []
    assert clock.sleeps == []
    assert len(http.requests) == (1 if phase == "metadata" else 2)
    assert http.clients[0].is_closed


def test_watch_times_out_while_draining_terminal_page():
    http = ScriptedHTTP([metadata("succeeded"), page(*range(1, 51), more=True)])

    outcome, observations = observe(http, FakeClock(), timeout=1)

    assert outcome == WatchOutcome("succeeded", 50, "timeout")
    assert observations == [("succeeded", list(range(1, 51)))]
    check_requests(http, [0])


def test_watch_request_budget_is_bounded_even_if_clock_stalls():
    http = ScriptedHTTP([metadata("running"), page(), metadata("running"), page()])
    clock = FakeClock()

    outcome, _ = observe(http, clock, timeout=3, pause=clock.sleeps.append)

    assert outcome == WatchOutcome("running", 0, "timeout")
    assert clock.sleeps == [2, 2]
    check_requests(http, [0, 0])


@pytest.fixture
def invoke_watch(monkeypatch):
    for variable in ("TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(variable, raising=False)

    def invoke(http, clock, *args, tty=False, json_output=True):
        def open_client(base_url):
            if tty:
                monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
            return http.open(base_url)

        monkeypatch.setattr(commands, "ForgeAPIClient", open_client)
        monkeypatch.setattr(
            commands, "watch_run", partial(watch_run, clock=clock.monotonic, pause=clock.sleep)
        )
        flags = ["--no-color", "--base-url", BASE_URL]
        if json_output:
            flags.append("--json")
        return runner.invoke(
            app, [*flags, "run", "watch", str(RUN_ID), *args],
            env={"TERM": "xterm-256color", "COLUMNS": "80", "FORCE_COLOR": "1"},
            color=True,
        )

    return invoke


@pytest.mark.parametrize("status, reason", [
    ("succeeded", "terminal"), ("failed", "terminal"), ("cancelled", "terminal"),
])
def test_watch_json_reports_observation_without_rich(invoke_watch, monkeypatch, status, reason):
    def forbidden_console(*args, **kwargs):
        pytest.fail("JSON watch must bypass Rich")

    monkeypatch.setattr(output, "Console", forbidden_console)
    http = ScriptedHTTP([metadata(status), page(3)])

    result = invoke_watch(http, FakeClock(), "--after-sequence", "2")

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "run_id": str(RUN_ID), "last_observed_status": status, "events": [event(3)],
        "next_after_sequence": 3, "stop_reason": reason,
    }
    check_requests(http, [2])


@pytest.mark.parametrize("tty", [False, True])
def test_watch_human_output_reports_changes_once_without_color(invoke_watch, tty):
    http = ScriptedHTTP([
        metadata("running"), page(1), metadata("running"), page(cursor=1),
        metadata("succeeded"), page(4),
    ])

    result = invoke_watch(http, FakeClock(), tty=tty, json_output=False)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.count("Observed status: running") == 1
    assert result.stdout.count("Observed status: succeeded") == 1
    assert result.stdout.count("#1  ") == result.stdout.count("#4  ") == 1
    assert "Last observed status: succeeded" in result.stdout
    assert "Next after sequence: 4" in result.stdout
    assert "Stop reason: terminal" in result.stdout
    assert "\x1b" not in result.stdout
    assert ("╭─ AgentRun watch " in result.stdout) is tty
    check_requests(http, [0, 1, 1])


@pytest.mark.parametrize("json_output", [False, True])
def test_watch_timeout_reports_deadline(invoke_watch, json_output):
    http = ScriptedHTTP([metadata("running"), page(3)])

    result = invoke_watch(http, FakeClock(), "--timeout", "1", json_output=json_output)

    assert result.exit_code == 6
    assert "deadline expired" in result.stderr
    if json_output:
        assert result.stdout == ""
    else:
        assert "Last observed status: running" in result.stdout
        assert "Next after sequence: 3" in result.stdout
        assert "Stop reason: timeout" in result.stdout
    check_requests(http, [0])


@pytest.mark.parametrize("phase", ["sleep", "request"])
@pytest.mark.parametrize("json_output", [False, True])
def test_watch_ctrl_c_stops_without_cancelling(invoke_watch, phase, json_output):
    def interrupt():
        raise KeyboardInterrupt

    clock = FakeClock(interrupt_sleep=phase == "sleep")
    replies = [metadata("running"), page(2)]
    if phase == "request":
        replies.append(interrupt)
    http = ScriptedHTTP(replies)

    result = invoke_watch(http, clock, json_output=json_output)

    assert result.exit_code == 130
    assert "observation interrupted" in result.stderr
    if json_output:
        assert result.stdout == ""
    else:
        assert "Last observed status: running" in result.stdout
        assert "Stop reason: interrupted" in result.stdout
    assert len(http.requests) == (2 if phase == "sleep" else 3)
    assert http.clients[0].is_closed


@pytest.mark.parametrize("code", [3, 4, 5])
@pytest.mark.parametrize("json_output", [False, True])
def test_watch_failures_keep_existing_exit_codes(invoke_watch, code, json_output):
    def timeout():
        raise httpx.ReadTimeout(MARKER)

    failures = {
        3: timeout,
        4: httpx.Response(404, json={"error": {"code": MARKER, "message": MARKER}}),
        5: httpx.Response(200, text=MARKER),
    }
    http = ScriptedHTTP([metadata("running"), page(2), metadata("running"), failures[code]])

    result = invoke_watch(http, FakeClock(), json_output=json_output)

    assert result.exit_code == code
    assert result.stderr
    assert MARKER not in result.stderr
    assert ("#2  " in result.stdout) is (not json_output)
    if json_output:
        assert result.stdout == ""
    check_requests(http, [0, 2])


def test_watch_rejects_repeated_sequence_without_emitting_json(invoke_watch):
    http = ScriptedHTTP([metadata("running"), page(2), metadata("succeeded"), page(2)])

    result = invoke_watch(http, FakeClock())

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "invalid response for GET /agent-runs/{run_id}/events" in result.stderr
    check_requests(http, [0, 2])


@pytest.mark.parametrize("args", [
    ["invalid"], [str(RUN_ID), "--interval", "0"], [str(RUN_ID), "--interval", "31"],
    [str(RUN_ID), "--timeout", "0"], [str(RUN_ID), "--timeout", "601"],
    [str(RUN_ID), "--after-sequence", "-1"], [str(RUN_ID), "--after-sequence", "2147483648"],
    [str(RUN_ID), "--timeout", "1.5"],
])
def test_watch_validates_arguments_before_client_creation(monkeypatch, args):
    def forbidden_client(*args, **kwargs):
        pytest.fail("Invalid arguments must not open a client")

    monkeypatch.setattr(commands, "ForgeAPIClient", forbidden_client)
    result = runner.invoke(app, ["run", "watch", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr


@pytest.mark.parametrize("args, expected", [
    (["run", "watch", "--help"], "--after-sequence"),
    (["run", "--help"], "Watch is read-only."),
    (["--version"], "0.1.0"),
])
def test_watch_help_and_version_stay_offline(monkeypatch, args, expected):
    def forbidden_client(*args, **kwargs):
        pytest.fail("Help and version must not open a client")

    monkeypatch.setattr(commands, "ForgeAPIClient", forbidden_client)
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert expected in result.stdout
    assert result.stderr == ""
