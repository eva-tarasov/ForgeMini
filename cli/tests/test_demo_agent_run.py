import asyncio
import json
import os
import shlex
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import demo_agent_run as demo


@pytest.mark.parametrize("args", [
    ["tcp"],
    ["tcp", "--approve-db", "forge"],
    ["live", "--approve-db", demo.DB],
    ["live", "--approve-db", demo.DB, "--approve-live-model", "another-model"],
    ["live", "--approve-live-model", demo.MODEL],
    ["active-cancel"],
    ["active-cancel", "--approve-db", demo.DB, "--approve-live-model", demo.MODEL],
    ["interactive", "--approve-db", "forge"],
    ["interactive", "--approve-db", demo.DB, "--approve-live-model", demo.MODEL],
])
def test_demo_refuses_unapproved_execution(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail("Argument checks must precede any demo execution")

    monkeypatch.setattr(demo, "run_demo", forbidden)
    with pytest.raises(SystemExit) as caught:
        demo.main(args)

    assert caught.value.code == 2


def test_demo_describe_only_prints_reviewed_environment(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Describing the demo must not execute it")

    monkeypatch.setattr(demo, "run_demo", forbidden)

    assert demo.main(["describe"]) == 0

    stdout, stderr = capsys.readouterr()
    budget = json.loads(stdout)
    assert stderr == ""
    assert budget["backend"] == "http://127.0.0.1:58066"
    assert budget["database"] == "forge_task06_week4_audit_4d6f653603"
    assert budget["model_requests_max"] == 3
    assert budget["completion_tokens_per_request_max"] == 512
    assert budget["scenario_timeout_seconds"] == 90
    assert budget["active_cancellation_model_requests_max"] == 1
    assert budget["cancellation_barrier_timeout_seconds"] == 5
    assert budget["interactive_session_seconds"] == 180


@pytest.mark.parametrize(("mode", "outcome"), [("active-cancel", "PASS"), ("interactive", "SESSION_ENDED")])
def test_demo_reports_mode_without_claiming_manual_pass(monkeypatch, capsys, mode, outcome):
    async def run_demo(selected_mode, report):
        assert selected_mode == mode
        report["cleanup"] = "completed"
        if mode == "interactive":
            report["manual_verification"] = "owner_pending"

    monkeypatch.setattr(demo, "run_demo", run_demo)
    monkeypatch.setattr(demo.logging, "disable", lambda _: None)
    assert demo.main([mode, "--approve-db", demo.DB]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == outcome
    assert report["cleanup"] == "completed"
    assert report.get("manual_verification") == ("owner_pending" if mode == "interactive" else None)


def test_interactive_launcher_requires_tty_before_setup(monkeypatch):
    monkeypatch.setattr(demo.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(demo.DemoError, match="interactive_launcher_requires_terminal"):
        asyncio.run(demo.run_demo("interactive", {}))


def test_terminal_cli_keeps_tty_environment_and_drops_credentials(monkeypatch, tmp_path):
    forge = tmp_path / "forge"
    forge.touch(mode=0o700)
    monkeypatch.setattr(demo, "CLI", forge)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("POSTGRES_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FORGE_BASE_URL", "https://outside.invalid")
    handoffs = []
    monkeypatch.setattr(demo.os, "execve", lambda *args: handoffs.append(args))

    assert demo.main(["terminal-cli", "--no-color", "workspace", "list"]) == 0
    [(binary, argv, env)] = handoffs
    assert binary == str(forge)
    assert argv == [str(forge), "--base-url", demo.BASE_URL, "--no-color", "workspace", "list"]
    assert env["TERM"] == "xterm-256color"
    assert env["COLUMNS"] == "40"
    assert {"OPENAI_API_KEY", "POSTGRES_PASSWORD", "FORGE_BASE_URL"}.isdisjoint(env)

    assert demo.main(["terminal-cli", "--base-url=https://outside.invalid", "workspace", "list"]) == 2
    assert len(handoffs) == 1


def test_provider_barrier_observes_cancellation_and_settled_execution():
    async def scenario():
        barrier = demo.CancellationBarrier()
        run_id = uuid4()
        coordinator = SimpleNamespace(_execution=None, _active_runs={run_id})
        async with httpx.AsyncClient(transport=httpx.MockTransport(barrier.run)) as client:
            task = asyncio.create_task(client.post("https://fixture.invalid/chat/completions"))
            coordinator._execution = task

            def release_slot(_task):
                coordinator._active_runs.remove(run_id)
                coordinator._execution = None

            task.add_done_callback(release_slot)
            try:
                await asyncio.wait_for(barrier.entered.wait(), 1)
                assert not task.done() and not barrier.cancelled.is_set()
                with pytest.raises(demo.DemoError, match="cancellation_barrier_entered_twice"):
                    await client.post("https://fixture.invalid/chat/completions")
                task.cancel()
                await demo.wait_execution_cleanup(coordinator, str(run_id), task, barrier)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_execution_cleanup_deadline_does_not_cancel_unsettled_task(monkeypatch):
    monkeypatch.setattr(demo, "SYNC_BUDGET", 0)

    async def scenario():
        barrier = demo.CancellationBarrier()
        task = asyncio.create_task(barrier.run(None))
        try:
            await barrier.entered.wait()
            with pytest.raises(TimeoutError):
                await demo.wait_execution_cleanup(None, str(uuid4()), task, barrier)
            assert not task.done() and not task.cancelling()
            assert not barrier.cancelled.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("second_method", ["GET", "POST"])
def test_watch_interrupt_requires_the_two_expected_gets(second_method):
    async def scenario():
        run_id = str(uuid4())
        path = f"/agent-runs/{run_id}"
        driver = demo.Driver(httpx)
        signals = []

        def record(request):
            driver.requests.append(request)
            driver.request_recorded.set()

        def send_signal(value):
            assert len(driver.requests) == 2
            signals.append(value)

        @asynccontextmanager
        async def process(*args):
            loop = asyncio.get_running_loop()
            loop.call_soon(record, ("GET", path, 200))
            loop.call_soon(record, (second_method, f"{path}/events", 200))
            yield SimpleNamespace(returncode=None, send_signal=send_signal)

        async def read_process(_process, label, exit_code, timeout):
            assert exit_code == 130
            driver.steps.append({"step": label, "exit": exit_code})

        driver.process = process
        driver.read_process = read_process
        if second_method == "GET":
            await demo.interrupt_watch(driver, run_id, "watch")
            assert signals == [signal.SIGINT]
            assert driver.steps == [{"step": "watch", "exit": 130, "http": [200, 200]}]
        else:
            with pytest.raises(demo.DemoError, match="watch_interrupt_requests"):
                await demo.interrupt_watch(driver, run_id, "watch")
            assert signals == []

    asyncio.run(scenario())


@pytest.mark.parametrize("send_return", [True, False])
def test_terminal_shutdown_on_return_or_deadline(monkeypatch, send_return):
    monkeypatch.setattr(demo, "INTERACTIVE_BUDGET", 1 if send_return else 0)
    read_fd, write_fd = os.pipe()
    try:
        if send_return:
            os.write(write_fd, b"\n")
        result = asyncio.run(demo.wait_for_terminal_shutdown(read_fd))
        assert result == ("owner_input" if send_return else "session_deadline")
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_interactive_session_prints_commands_for_its_fixture_only(monkeypatch, capsys):
    workspace_id, task_id, run_id = (str(uuid4()) for _ in range(3))
    driver = demo.Driver(httpx)
    report = {}

    async def stop(fd):
        assert fd == 42
        return "owner_input"

    monkeypatch.setattr(demo.sys, "stdin", SimpleNamespace(fileno=lambda: 42))
    monkeypatch.setattr(demo, "wait_for_terminal_shutdown", stop)
    asyncio.run(demo.interactive_session(driver, workspace_id, task_id, run_id, report))

    output = capsys.readouterr().out
    commands = [shlex.split(line)[3:] for line in output.splitlines() if " terminal-cli " in line]
    assert f"Workspace {workspace_id}" in output
    assert ["task", "get", task_id] in commands
    assert ["run", "watch", run_id, "--interval", "1", "--timeout", "20"] in commands
    assert ["run", "cancel", run_id] in commands
    assert ["--json", "run", "get", run_id] in commands
    assert ["--no-color", "run", "get", run_id] in commands
    assert {path for method, path in driver.allowed_requests if method == "POST"} == {
        f"/agent-runs/{run_id}/start", f"/agent-runs/{run_id}/cancel",
    }
    assert report["manual_verification"] == "owner_pending"
