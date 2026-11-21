"""Opt-in CLI-05 demo over real loopback HTTP and a verified disposable DB."""

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import socket
import sys
from contextlib import asynccontextmanager, nullcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli/.venv/bin/forge"
HOST = "127.0.0.1"
PORT = 58066
BASE_URL = f"http://{HOST}:{PORT}"
DB = "forge_task06_week4_audit_4d6f653603"
MODEL = "gpt-6-luna"
RUN_BUDGET = 90
COMMAND_BUDGET = 10
WATCH_BUDGET = 25
SYNC_BUDGET = 5
INTERACTIVE_BUDGET = 180


class DemoError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise DemoError(code)


def configuration():
    return {
        "backend": BASE_URL,
        "database": DB,
        "database_address": "127.0.0.1:50088",
        "database_version": "17.11",
        "alembic_revision": "1d972cc34fbe",
        "model": MODEL,
        "model_requests_max": 3,
        "completion_tokens_per_request_max": 512,
        "body_bytes_per_request_max": 16384,
        "model_http_timeout_seconds": 10,
        "scenario_timeout_seconds": RUN_BUDGET,
        "cli_timeout_seconds": COMMAND_BUDGET,
        "watch_subprocess_timeout_seconds": WATCH_BUDGET,
        "active_cancellation_model_requests_max": 1,
        "cancellation_barrier_timeout_seconds": SYNC_BUDGET,
        "interactive_session_seconds": INTERACTIVE_BUDGET,
        "fixture": "one synthetic sample.txt, repository read only",
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        epilog="terminal-cli runs the installed Forge in the current TTY with a clean environment.",
    )
    parser.add_argument("mode", choices=("describe", "tcp", "live", "active-cancel", "interactive"))
    parser.add_argument("--approve-db")
    parser.add_argument("--approve-live-model")
    args = parser.parse_args(argv)
    if args.mode != "describe" and args.approve_db != DB:
        parser.error("Running requires explicit approval of the named fixture database.")
    if args.mode == "live" and args.approve_live_model != MODEL:
        parser.error("Live mode requires explicit approval of the named paid model.")
    if args.mode != "live" and args.approve_live_model is not None:
        parser.error("Only live mode accepts paid-model approval.")
    return args


def cli_environment():
    keys = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR", "COLUMNS", "LINES")
    return {key: os.environ[key] for key in keys if key in os.environ}


def terminal_cli(argv):
    require(CLI.is_file() and os.access(CLI, os.X_OK), "forge_cli_not_installed")
    require(not any(arg == "--base-url" or arg.startswith("--base-url=") for arg in argv),
            "terminal_cli_base_url_is_fixed")
    os.execve(str(CLI), [str(CLI), "--base-url", BASE_URL, *argv], cli_environment())


class Driver:
    def __init__(self, httpx):
        self.httpx = httpx
        self.requests = []
        self.request_recorded = asyncio.Event()
        self.steps = []
        self.allowed_requests = None

    def attach(self, app, faults=False):
        from fastapi.responses import JSONResponse

        @app.middleware("http")
        async def record(request, call_next):
            # Only the offline TCP matrix serves these intentional faults.
            fault_read = faults and request.method == "GET"
            if self.allowed_requests is not None and (request.method, request.url.path) not in self.allowed_requests:
                response = JSONResponse({"error": {"code": "NOT_FOUND", "message": "Not found"}}, status_code=404)
            elif fault_read and request.url.path == "/agent-runs/00000000-0000-0000-0000-000000000001":
                response = JSONResponse({"unexpected": "synthetic-private-payload"})
            elif fault_read and request.url.path == "/agent-runs/00000000-0000-0000-0000-000000000002/result":
                response = JSONResponse(
                    {"error": {"code": "synthetic-private-code", "message": "synthetic-private-payload"}},
                    status_code=500,
                )
            else:
                response = await call_next(request)
            self.requests.append((request.method, request.url.path, response.status_code))
            self.request_recorded.set()
            return response

    @asynccontextmanager
    async def process(self, *command, json_output=True):
        # CLI children do not inherit provider or PostgreSQL credentials.
        flags = ["--json"] if json_output else []
        process = await asyncio.create_subprocess_exec(
            str(CLI), "--base-url", BASE_URL, "--no-color", *flags, *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cli_environment(),
        )
        try:
            yield process
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def read_process(self, process, label, exit_code, timeout):
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
        self.steps.append({"step": label, "exit": process.returncode})
        require(process.returncode == exit_code, f"{label}:unexpected_cli_exit")
        if exit_code:
            require(not stdout and stderr, f"{label}:invalid_error_streams")
            for value in (b"synthetic-private", BASE_URL.encode(), b"Traceback"):
                require(value not in stderr, f"{label}:unsafe_diagnostic")
        else:
            require(not stderr and stdout, f"{label}:invalid_success_streams")
        return stdout

    async def cli(self, label, *command, status=None, exit_code=0):
        before = len(self.requests)
        async with self.process(*command) as process:
            stdout = await self.read_process(process, label, exit_code, COMMAND_BUDGET)
        seen = [item[2] for item in self.requests[before:]]
        self.steps[-1]["http"] = seen
        require(seen == ([] if status is None else [status]), f"{label}:unexpected_http_statuses")
        return json.loads(stdout) if not exit_code else None

    async def backend_validation(self, run_id):
        async with self.httpx.AsyncClient(base_url=BASE_URL, trust_env=False, timeout=5) as client:
            response = await client.get(f"/agent-runs/{run_id}/events", params={"limit": 0})
        require(response.status_code == 422, "server_validation_expected_422")
        require(response.json()["error"]["code"] == "VALIDATION_ERROR", "server_validation_error_contract")
        self.steps.append({"step": "server_invalid_cursor_limit", "http": [response.status_code]})


def loopback_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
    except OSError:
        listener.close()
        raise DemoError("loopback_port_unavailable") from None
    return listener


@asynccontextmanager
async def serve(app, uvicorn):
    # Bind explicitly so no other service can receive the demo CLI requests.
    with loopback_socket() as listener:
        server = uvicorn.Server(uvicorn.Config(
            app, host=HOST, port=PORT, workers=1, lifespan="on", access_log=False,
            log_config=None, timeout_graceful_shutdown=5, timeout_keep_alive=1,
        ))
        server.capture_signals = nullcontext
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    require(not task.done(), "backend_start_failed")
                    await asyncio.sleep(0.02)
            yield
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=8)


async def make_run(driver, smoke, workspace_id, label):
    task = await driver.cli(
        f"{label}_task", "task", "create", "--workspace-id", workspace_id,
        "--goal", smoke.GOAL, "--criterion", smoke.CRITERIA[0], status=201,
    )
    run = await driver.cli(
        f"{label}_run", "run", "create", "--task-id", task["id"], status=201,
    )
    require(run["status"] == "queued" and run["task_id"] == task["id"], f"{label}:initial_metadata")
    return task["id"], run["id"]


async def interrupt_watch(driver, run_id, label):
    before = len(driver.requests)
    expected = [("GET", f"/agent-runs/{run_id}", 200), ("GET", f"/agent-runs/{run_id}/events", 200)]
    async with driver.process("run", "watch", run_id, "--interval", "30", "--timeout", "40") as process:
        async with asyncio.timeout(SYNC_BUDGET):
            while len(driver.requests) < before + len(expected):
                driver.request_recorded.clear()
                await driver.request_recorded.wait()
        require(driver.requests[before:] == expected, "watch_interrupt_requests")
        require(process.returncode is None, "watch_exited_before_interrupt")
        process.send_signal(signal.SIGINT)
        await driver.read_process(process, label, 130, COMMAND_BUDGET)
    require(driver.requests[before:] == expected, "watch_interrupt_requests")
    driver.steps[-1]["http"] = [200, 200]


async def offline_failures(driver, cancelled_task, queued_run):
    absent = str(uuid4())
    for operation in ("get", "start", "cancel", "events", "result", "watch"):
        await driver.cli(f"{operation}_404", "run", operation, absent, status=404, exit_code=4)
    await driver.cli("create_404", "run", "create", "--task-id", absent, status=404, exit_code=4)
    await driver.cli("invalid_uuid", "run", "get", "invalid", exit_code=2)
    await driver.cli("invalid_event_limit", "run", "events", absent, "--limit", "101", exit_code=2)
    await driver.cli("invalid_watch_timeout", "run", "watch", absent, "--timeout", "0", exit_code=2)
    await driver.backend_validation(queued_run)
    await driver.cli("result_500_fault", "run", "result", "00000000-0000-0000-0000-000000000002",
                     status=500, exit_code=4)
    await driver.cli("get_malformed_200_fault", "run", "get", "00000000-0000-0000-0000-000000000001",
                     status=200, exit_code=5)
    before = len(driver.requests)
    async with driver.process("run", "watch", queued_run, "--timeout", "1") as process:
        await driver.read_process(process, "watch_deadline", 6, COMMAND_BUDGET)
    require(driver.requests[before:] == [
        ("GET", f"/agent-runs/{queued_run}", 200),
        ("GET", f"/agent-runs/{queued_run}/events", 200),
    ], "watch_deadline_requests")

    await interrupt_watch(driver, queued_run, "watch_ctrl_c")

    cancelled = await driver.cli("cancel", "run", "cancel", queued_run, status=200)
    require(cancelled["status"] == "cancelled" and cancelled["id"] == queued_run, "cancelled_metadata")
    await driver.cli("cancel_again", "run", "cancel", queued_run, status=200)
    await driver.cli("create_after_cancel_409", "run", "create", "--task-id", cancelled_task,
                     status=409, exit_code=4)
    async with driver.process("run", "watch", queued_run, "--timeout", "5", json_output=False) as process:
        stdout = await driver.read_process(process, "watch_cancelled", 0, COMMAND_BUDGET)
    require(b"Last observed status: cancelled" in stdout and b"Stop reason: terminal" in stdout,
            "watch_cancelled_snapshot")


async def verify_fixture(smoke, factory, binding, task_id, run_id):
    identity = smoke.VerificationBinding(
        UUID(run_id), UUID(task_id), binding.workspace_id, binding.baseline.revision,
    )
    plan = smoke.VerificationPlan(
        uuid4(), identity, smoke.fingerprint_criteria(smoke.CRITERIA),
        (smoke.FileDigestCheck(uuid4(), 0, smoke.FILE, sha256(smoke.CONTENT).hexdigest()),),
    )
    access = smoke.TrustedFixtureAccess(
        plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=identity,
        root=binding.root, read_scopes=binding.read_scopes, acceptance_criteria=smoke.CRITERIA,
    )
    evidence = await smoke.VerificationService(factory).verify(
        plan, access, attempt_id=uuid4(), isolated_fixture=True,
    )
    require(evidence.verdict is smoke.VerificationVerdict.PASS, "independent_verification_not_pass")
    return evidence.verdict.value


async def observe_and_verify(driver, smoke, factory, binding, task_id, run_id, release):
    release.set()
    async with asyncio.timeout(50):
        while True:
            result = await driver.cli("preliminary_result", "run", "result", run_id, status=200)
            driver.steps[-1]["observed_status"] = result["run_status"]
            require(result["run_status"] in ("queued", "running", "verifying"),
                    "unexpected_run_state_before_verification")
            require(result["verification_verdict"] is None, "premature_verification")
            if result["run_status"] == "verifying" and result["usage"] is not None:
                break
            await asyncio.sleep(0.25)
    require(result["preliminary_status"] == "ready_for_verification", "missing_preliminary_result")

    before = len(driver.requests)
    async with driver.process("run", "watch", run_id, "--interval", "1", "--timeout", "15") as watcher:
        async with asyncio.timeout(5):
            while len(driver.requests) < before + 2:
                require(watcher.returncode is None, "watch_exited_before_verification")
                await asyncio.sleep(0.02)
        verdict = await verify_fixture(smoke, factory, binding, task_id, run_id)
        stdout = await driver.read_process(watcher, "watch_verified", 0, WATCH_BUDGET)
    watch_requests = driver.requests[before:]
    cycle = [("GET", f"/agent-runs/{run_id}", 200), ("GET", f"/agent-runs/{run_id}/events", 200)]
    require(watch_requests == cycle * (len(watch_requests) // 2), "observation_not_read_only")
    driver.steps[-1]["http"] = [status for _, _, status in watch_requests]
    watched = json.loads(stdout)
    sequences = [event["sequence"] for event in watched["events"]]
    require(
        watched["run_id"] == run_id and watched["last_observed_status"] == "succeeded"
        and watched["stop_reason"] == "terminal" and sequences == sorted(set(sequences))
        and bool(sequences) and watched["next_after_sequence"] == sequences[-1]
        and any(event["kind"] == "verification_recorded" and event["details"] == {"verdict": verdict}
                for event in watched["events"]),
        "watch_verified_metadata",
    )
    final = await driver.cli("verified_result", "run", "result", run_id, status=200)
    require(final["run_status"] == "succeeded" and final["verification_verdict"] == verdict,
            "missing_verified_terminal_result")
    return final


async def run_fixture_commands(driver, smoke, factory, binding, release, mode_is_live, report):
    workspace_id = str(binding.workspace_id)
    task_id, run_id = await make_run(driver, smoke, workspace_id, "primary")
    replayed = await driver.cli("queued_replay", "run", "create", "--task-id", task_id, status=200)
    require(replayed["id"] == run_id and replayed["status"] == "queued", "queued_replay_mismatch")
    if not mode_is_live:
        cancel_task, cancel_run = await make_run(driver, smoke, workspace_id, "cancel")
        await offline_failures(driver, cancel_task, cancel_run)
        _, capacity_run = await make_run(driver, smoke, workspace_id, "capacity")
    accepted = await driver.cli("start", "run", "start", run_id, status=202)
    require(accepted == {"run_id": run_id, "accepted": True}, "start_acceptance")
    if not mode_is_live:
        await driver.cli("duplicate_start_409", "run", "start", run_id, status=409, exit_code=4)
        await driver.cli("capacity_429", "run", "start", capacity_run, status=429, exit_code=4)
    info = await driver.cli("get_metadata", "run", "get", run_id, status=200)
    require(info["id"] == run_id and info["task_id"] == task_id, "wrong_run_metadata")
    result = await observe_and_verify(driver, smoke, factory, binding, task_id, run_id, release)
    page = await driver.cli(
        "events_first_page", "run", "events", run_id, "--limit", "1", status=200
    )
    require(len(page["items"]) == 1 and page["has_more"], "events_expected_first_page")
    tail = await driver.cli(
        "events_next_page", "run", "events", run_id, "--after-sequence",
        str(page["next_after_sequence"]), status=200,
    )
    require(
        tail["items"] and not tail["has_more"]
        and all(item["sequence"] > page["next_after_sequence"] for item in tail["items"])
        and any(item["kind"] == "verification_recorded" for item in tail["items"]),
        "events_tail_did_not_advance_to_verification",
    )
    if not mode_is_live:
        await driver.cli("cancel_terminal_409", "run", "cancel", run_id, status=409, exit_code=4)
        await driver.cli("cancel_capacity_fixture", "run", "cancel", capacity_run, status=200)
    audit = await smoke.read_tool_audit(factory, UUID(run_id))
    require(
        len(audit) == 1 and audit[0].tool_name == smoke.SMOKE_TOOL_NAME
        and audit[0].status is smoke.ToolCallStatus.SUCCEEDED
        and audit[0].finished is not None,
        "missing_completed_read_only_tool_call",
    )
    report.update(
        run_status=result["run_status"],
        verification_verdict=result["verification_verdict"],
        usage=result["usage"],
        tool_audit=[call.safe_details() for call in audit],
    )


class CancellationBarrier:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, _request):
        require(not self.entered.is_set(), "cancellation_barrier_entered_twice")
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def start_at_barrier(driver, coordinator, run_id, barrier, label):
    accepted = await driver.cli(f"{label}_start", "run", "start", run_id, status=202)
    require(accepted == {"run_id": run_id, "accepted": True}, f"{label}:start_acceptance")
    await asyncio.wait_for(barrier.entered.wait(), SYNC_BUDGET)
    active = coordinator._execution
    require(active is not None and active.run_id == UUID(run_id) and not active.task.done(),
            f"{label}:execution_not_active")
    return active.task


async def wait_execution_cleanup(coordinator, run_id, task, barrier):
    settled = asyncio.Event()
    task.add_done_callback(lambda _: settled.set())
    async with asyncio.timeout(SYNC_BUDGET):
        await barrier.cancelled.wait()
        await settled.wait()
    require(task.cancelled() and coordinator._execution is None
            and UUID(run_id) not in coordinator._active_runs, "execution_not_cleaned_up")


async def run_active_cancellation(driver, smoke, factory, binding, coordinator, barrier, probes, report):
    workspace_id = str(binding.workspace_id)
    task_id, run_id = await make_run(driver, smoke, workspace_id, "active")
    _, probe_id = await make_run(driver, smoke, workspace_id, "capacity_probe")
    probe = probes[UUID(probe_id)] = CancellationBarrier()

    execution = await start_at_barrier(driver, coordinator, run_id, barrier, "active")
    running = await driver.cli("committed_running", "run", "get", run_id, status=200)
    require(running["status"] == "running" and running["task_id"] == task_id, "run_not_committed_running")
    await interrupt_watch(driver, run_id, "active_watch_ctrl_c")
    still_running = await driver.cli("metadata_after_sigint", "run", "get", run_id, status=200)
    require(still_running == running and not execution.done() and not execution.cancelling()
            and not barrier.cancelled.is_set() and UUID(run_id) in coordinator._active_runs,
            "watch_interrupted_backend_execution")
    await driver.cli("capacity_while_active", "run", "start", probe_id, status=429, exit_code=4)
    require(not probe.entered.is_set(), "probe_executed_without_capacity")

    cancelled = await driver.cli("cancel_active", "run", "cancel", run_id, status=200)
    require(cancelled == {**running, "status": "cancelled"}, "cancel_active_metadata")
    await wait_execution_cleanup(coordinator, run_id, execution, barrier)
    repeated = await driver.cli("repeat_active_cancel", "run", "cancel", run_id, status=200)
    require(repeated == cancelled, "repeat_active_cancel_changed_metadata")

    probe_task = await start_at_barrier(driver, coordinator, probe_id, probe, "released_capacity")
    probe_cancel = await driver.cli("cancel_capacity_probe", "run", "cancel", probe_id, status=200)
    require(probe_cancel["status"] == "cancelled", "probe_cancel_status")
    await wait_execution_cleanup(coordinator, probe_id, probe_task, probe)

    persisted = await driver.cli("persisted_cancelled_metadata", "run", "get", run_id, status=200)
    require(persisted == cancelled, "cancelled_metadata_not_persisted")
    page = await driver.cli("persisted_cancelled_events", "run", "events", run_id, status=200)
    require(not page["has_more"], "cancelled_event_history_incomplete")
    transitions = [event["details"] for event in page["items"] if event["kind"] == "agent_run_status_changed"]
    require(transitions == [{"from": "queued", "to": "running"}, {"from": "running", "to": "cancelled"}],
            "unexpected_cancelled_transitions")
    require(all(event["kind"] in ("agent_run_status_changed", "agent_usage_recorded") for event in page["items"]),
            "unexpected_cancelled_events")
    result = await driver.cli("persisted_cancelled_result", "run", "result", run_id, status=200)
    require(result["run_status"] == "cancelled" and result["run_id"] == run_id
            and all(result[key] is None for key in
                    ("preliminary_status", "verification_verdict", "execution_failure_code")),
            "unexpected_cancelled_result")
    require(not await smoke.read_tool_audit(factory, UUID(run_id)), "cancelled_run_has_tool_audit")
    report.update(
        watch_interrupted_with_execution_active=True,
        provider_observed_cancellation=True,
        execution_cleanup="observed",
        execution_capacity_released=True,
        run_status=result["run_status"],
        transitions=transitions,
        verification_verdict=result["verification_verdict"],
        usage=result["usage"],
    )


def terminal_command(*args):
    return shlex.join([str(CLI.with_name("python")), str(Path(__file__).resolve()), "terminal-cli", *args])


async def wait_for_terminal_shutdown(fd):
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    loop.add_reader(fd, ready.set)
    try:
        async with asyncio.timeout(INTERACTIVE_BUDGET):
            await ready.wait()
            os.read(fd, 4096)
    except TimeoutError:
        return "session_deadline"
    finally:
        loop.remove_reader(fd)
    return "owner_input"


async def interactive_session(driver, workspace_id, task_id, run_id, report):
    path = f"/agent-runs/{run_id}"
    driver.allowed_requests = {
        ("GET", "/workspaces"), ("GET", f"/tasks/{task_id}"), ("GET", path),
        ("GET", f"{path}/events"), ("GET", f"{path}/result"),
        ("POST", f"{path}/start"), ("POST", f"{path}/cancel"),
    }
    before = len(driver.requests)
    report["manual_verification"] = "owner_pending"
    print(f"READY at {BASE_URL} for {INTERACTIVE_BUDGET} seconds. Press Return here to shut down.", flush=True)
    print(f"Workspace {workspace_id}\nTask {task_id}\nAgentRun {run_id}")
    print("\nIn Terminal 2, inspect the queued fixture. Temporarily narrow the window and repeat run get.")
    for args in (
        ("workspace", "list"), ("task", "get", task_id), ("run", "get", run_id),
        ("--json", "run", "get", run_id), ("--no-color", "run", "get", run_id),
    ):
        print(terminal_command(*args))
    print("\nRun these next. After watch starts, press Ctrl-C in Terminal 2 and immediately run the next line.")
    print("The backend execution deadline is 60 seconds from start. Check running before explicit cancel.")
    for args in (
        ("run", "start", run_id), ("run", "watch", run_id, "--interval", "1", "--timeout", "20"),
        ("run", "get", run_id), ("run", "cancel", run_id), ("run", "events", run_id),
        ("run", "result", run_id), ("run", "watch", run_id, "--timeout", "5"),
    ):
        print(terminal_command(*args), flush=True)
    report["session_end"] = await wait_for_terminal_shutdown(sys.stdin.fileno())
    report["session_http"] = [
        {"method": method, "path": path, "status": status}
        for method, path, status in driver.requests[before:]
        if (method, path) in driver.allowed_requests
    ]


async def guard_empty_database(smoke, factory, tables):
    await smoke.verify_db(factory)
    async with asyncio.timeout(5), factory.begin() as session:
        await session.execute(smoke.text("SET TRANSACTION READ ONLY"))
        for table in tables:
            require(
                await session.scalar(smoke.select(table.id).limit(1)) is None,
                "fixture_database_contains_business_rows",
            )


async def cleanup_workspace(smoke, factory, name, artifact):
    await smoke.verify_db(factory)
    async with asyncio.timeout(5), factory.begin() as session:
        workspaces = smoke.select(smoke.WorkspaceModel.id).where(smoke.WorkspaceModel.name == name)
        tasks = smoke.select(smoke.TaskModel.id).where(smoke.TaskModel.workspace_id.in_(workspaces))
        runs = smoke.select(smoke.AgentRunModel.id).where(smoke.AgentRunModel.task_id.in_(tasks))
        calls = smoke.select(smoke.ToolCallModel.id).where(smoke.ToolCallModel.agent_run_id.in_(runs))
        await session.execute(smoke.delete(smoke.ApprovalModel).where(smoke.ApprovalModel.tool_call_id.in_(calls)))
        for table in (smoke.ToolCallModel, smoke.AgentEventModel, artifact):
            await session.execute(smoke.delete(table).where(table.agent_run_id.in_(runs)))
        await session.execute(smoke.delete(smoke.AgentRunModel).where(smoke.AgentRunModel.task_id.in_(tasks)))
        await session.execute(smoke.delete(smoke.TaskModel).where(smoke.TaskModel.workspace_id.in_(workspaces)))
        await session.execute(smoke.delete(smoke.WorkspaceModel).where(smoke.WorkspaceModel.name == name))
        require(await session.scalar(workspaces) is None, "fixture_workspace_not_removed")


async def run_demo(mode, report):
    live = mode == "live"
    if mode == "interactive":
        require(sys.stdin.isatty() and sys.stdout.isatty(), "interactive_launcher_requires_terminal")

    import httpx
    import uvicorn
    from app.db.models.execution import ArtifactModel
    from scripts import week4_live_smoke as smoke

    require(Path(smoke.__file__).resolve() == ROOT / "backend/scripts/week4_live_smoke.py", "backend_source_mismatch")
    require(CLI.is_file() and os.access(CLI, os.X_OK), "forge_cli_not_installed")
    require((smoke.TEST_DB, smoke.MODEL, smoke.MAX_REQUESTS, smoke.MAX_OUTPUT_TOKENS,
             smoke.MAX_REQUEST_BYTES, smoke.REQUEST_TIMEOUT) == (DB, MODEL, 3, 512, 16384, 10.0),
            "reviewed_budget_or_fixture_changed")
    try:
        settings = smoke.load_db_settings()
        model_settings = smoke.load_model_settings(live)
    except smoke.SmokeError as error:
        raise DemoError(str(error)) from None
    driver = Driver(httpx)
    report["steps"] = driver.steps
    with loopback_socket():
        if mode == "tcp":
            await driver.cli("connection_refused", "run", "get", str(uuid4()), exit_code=3)

    engine = smoke.create_engine(settings)
    factory = smoke.create_session_factory(engine)
    lock = factory()
    name = f"CLI05 demo {uuid4()}"
    release = asyncio.Event()
    barrier = CancellationBarrier() if mode in ("active-cancel", "interactive") else None
    probes = {}
    tables = (smoke.WorkspaceModel, smoke.TaskModel, smoke.AgentRunModel, smoke.AgentEventModel,
              smoke.ToolCallModel, smoke.ApprovalModel, ArtifactModel)
    try:
        await smoke.verify_db(factory)
        async with asyncio.timeout(5):
            acquired = await lock.scalar(smoke.text("SELECT pg_try_advisory_xact_lock(516058066)"))
            require(acquired is True, "fixture_database_demo_lock_busy")
        await guard_empty_database(smoke, factory, tables)
        report["workspace_marker"] = name
        try:
            async with asyncio.timeout(RUN_BUDGET) as scenario_deadline:
                bootstrap = smoke.create_app(session_factory=factory)
                driver.attach(bootstrap)
                async with serve(bootstrap, uvicorn):
                    health = await driver.cli("health", "health", status=200)
                    require(health["status"] == "ok", "health_contract")
                    if mode == "tcp":
                        for operation in ("start", "cancel"):
                            await driver.cli(f"unbound_{operation}_503", "run", operation, str(uuid4()),
                                             status=503, exit_code=4)
                    workspace = await driver.cli("workspace", "workspace", "create", "--name", name, status=201)
                    require(workspace["name"] == name, "fixture_workspace_name_mismatch")

                async def offline_reply(request):
                    await asyncio.wait_for(release.wait(), timeout=5)
                    return smoke.offline_reply(request)

                url = smoke.OPENAI_URL if live else smoke.OFFLINE_URL
                if live:
                    inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                else:
                    inner = httpx.MockTransport(barrier.run if barrier is not None else offline_reply)
                transport = smoke.BoundedTransport(url, inner)
                with TemporaryDirectory(prefix="forge-cli05-demo-") as temp, smoke.tracing_context(enabled=False, parent=False):
                    root = Path(temp).resolve()
                    fixture = root / smoke.FILE
                    fixture.write_bytes(smoke.CONTENT)
                    fixture.chmod(0o444)
                    binding = smoke.RepositoryFixtureBinding(
                        UUID(workspace["id"]), root, smoke.FixtureBaseline("smoke-v1", {smoke.FILE: smoke.CONTENT.decode()}),
                        smoke.READ_ONLY, (smoke.PathScope(smoke.FILE),),
                    )

                    def build_runner(session_factory, run_id):
                        if run_id in probes:
                            return probes[run_id]
                        return smoke.build_runner(session_factory, model, binding)

                    with httpx.Client(transport=smoke.ClosedSyncTransport(), trust_env=False) as sync_client:
                        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as async_client:
                            model = smoke.create_bounded_model(model_settings, url, sync_client, async_client)
                            app = smoke.create_app(
                                session_factory=factory,
                                execution_binding=smoke.RunExecutionBinding(binding.workspace_id, build_runner),
                            )
                            driver.attach(app, faults=mode == "tcp")
                            async with serve(app, uvicorn):
                                try:
                                    if mode == "active-cancel":
                                        await run_active_cancellation(
                                            driver, smoke, factory, binding, app.state.run_coordinator,
                                            barrier, probes, report,
                                        )
                                        expected_calls = {1}
                                    elif mode == "interactive":
                                        workspace_id = str(binding.workspace_id)
                                        task_id, run_id = await make_run(driver, smoke, workspace_id, "interactive")
                                        scenario_deadline.reschedule(None)
                                        await interactive_session(driver, workspace_id, task_id, run_id, report)
                                        expected_calls = {0, 1}
                                    else:
                                        await run_fixture_commands(
                                            driver, smoke, factory, binding, release, live, report,
                                        )
                                        expected_calls = {3}
                                finally:
                                    release.set()
                                    report["model_requests"] = transport.calls
                            report["model_requests"] = transport.calls
                            require(list(fixture.parent.iterdir()) == [fixture] and fixture.read_bytes() == smoke.CONTENT,
                                    "repository_fixture_changed")
                            report["fixture_sha256"] = sha256(fixture.read_bytes()).hexdigest()
                            require(transport.calls in expected_calls and transport.refusal_code is None,
                                    "model_budget_or_path_mismatch")
        finally:
            report["cleanup"] = "unconfirmed"
            await cleanup_workspace(smoke, factory, name, ArtifactModel)
            await guard_empty_database(smoke, factory, tables)
            report["cleanup"] = "completed"
    except smoke.SmokeError as error:
        raise DemoError(str(error)) from None
    finally:
        async with asyncio.timeout(5):
            await lock.close()
            await engine.dispose()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "terminal-cli":
        try:
            terminal_cli(argv[1:])
        except DemoError as error:
            print(error, file=sys.stderr)
            return 2
        return 0
    args = arguments(argv)
    if args.mode == "describe":
        print(json.dumps(configuration(), sort_keys=True))
        return 0
    logging.disable(logging.CRITICAL)
    report = {"mode": args.mode, "outcome": "FAILED", "cleanup": "not_started"}
    try:
        asyncio.run(run_demo(args.mode, report))
    except DemoError as error:
        report["failure"] = str(error)
    except TimeoutError:
        report["failure"] = "deadline_exceeded"
    except KeyboardInterrupt:
        report["failure"] = "interrupted"
    except Exception:
        report["failure"] = "configuration_or_runtime_error"
    else:
        report["outcome"] = "SESSION_ENDED" if args.mode == "interactive" else "PASS"
    print(json.dumps(report, sort_keys=True))
    return 1 if report["outcome"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
