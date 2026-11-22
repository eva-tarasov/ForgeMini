"""Public run lifecycle over real HTTP and PostgreSQL: start, cancel, capacity, shutdown, failure handling.

Ported in ADR-003 (C3) from the M1-era `test_agent_runs_start_cancel_integration`: the kernel behaviour is kept, the
LangGraph/approval/tool-call runners are replaced by plain doubles, because the prototype's runner is the sandbox loop.
Opt-in like the other task06 integration tests (FORGE_TASK06_TEST_DB + POSTGRES_DB name the same forge_task06_* database).
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text

from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.repositories.agent_events import AgentEventRepository
from app.services import in_process_run_coordinator
from app.services.agent_result import AgentFinalResult
from app.services.agent_runs import AgentRunService
from app.services.agent_service import AgentService
from app.services.in_process_run_coordinator import RunExecutionBinding
from run_harness import make_run_scenario

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to the dedicated forge_task06_* database",
)
FINAL = AgentFinalResult(
    summary="Synthetic preliminary result", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)


@pytest.fixture
def run_scenario():
    return make_run_scenario()


def _app(case, build_runner, *, workspace_id=None):
    return create_app(
        session_factory=case.factory,
        execution_binding=RunExecutionBinding(workspace_id or case.workspace_id, build_runner),
    )


@asynccontextmanager
async def _client(app, *, transport_app=None):
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=transport_app or app, raise_app_exceptions=False),
        base_url="http://forge-tests.invalid", trust_env=False,
    ) as client:
        yield client


async def _command(client, run_id, command, **kwargs):
    return await client.post(f"/agent-runs/{run_id}/{command}", **kwargs)


def _assert_error(response, status, code):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def _transitions(events):
    return [(item.payload["from"], item.payload["to"]) for item in events
            if item.event_type == "agent_run_status_changed"]


def _execution(app):
    active = app.state.run_coordinator._execution
    assert active is not None
    return active.task


async def _wait_for_cleanup(app, task):
    cleaned = asyncio.Event()
    task.add_done_callback(lambda _: cleaned.set())
    await asyncio.wait_for(cleaned.wait(), timeout=5)
    assert task.done() and app.state.run_coordinator._execution is None


async def _snapshot(factory, case):
    """(run, parent task, events) as committed right now, read through an independent session."""
    async with factory() as session:
        run = await session.get(AgentRunModel, case.run_id)
        parent = await session.get(TaskModel, case.task_id)
        events = (await session.scalars(
            select(AgentEventModel).where(AgentEventModel.agent_run_id == case.run_id)
            .order_by(AgentEventModel.sequence)
        )).all()
        return run, parent, events


class _PausedRunner:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, request):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return FINAL


def test_http_start_accepts_before_claim_and_runner_result_reaches_verifying(run_scenario, monkeypatch):
    async def check(case):
        preclaim, release = asyncio.Event(), asyncio.Event()
        claim = AgentService._claim
        claims = []

        async def paused(service, run_id):
            claims.append(run_id)
            preclaim.set()
            await release.wait()
            return await claim(service, run_id)

        class Runner:
            async def run(self, request):
                assert (request.run_id, request.task_id) == (case.run_id, case.task_id)
                return FINAL

        monkeypatch.setattr(AgentService, "_claim", paused)
        app = _app(case, lambda *_: Runner())
        async with _client(app) as client:
            start = await _command(client, case.run_id, "start")
            assert start.status_code == 202
            assert start.json() == {"run_id": str(case.run_id), "accepted": True}
            assert start.headers["Location"] == f"/agent-runs/{case.run_id}"
            await preclaim.wait()
            task = _execution(app)
            try:
                waiting, _, events = await _snapshot(case.factory, case)
                assert waiting.status == "queued" and not events        # 202 came BEFORE the claim
                assert (await client.get(start.headers["Location"])).json()["status"] == "queued"
            finally:
                release.set()
            await _wait_for_cleanup(app, task)
            assert claims == [case.run_id]
            run, parent, events = await _snapshot(case.factory, case)
            assert (run.status, parent.status) == ("verifying", "queued")
            assert _transitions(events) == [("queued", "running"), ("running", "verifying")]
            assert run.final_result["result"]["summary"] == FINAL.summary
    run_scenario(check)


def test_http_concurrent_start_conflicts_and_capacity_allows_one_claim(run_scenario, monkeypatch):
    async def check(case):
        runner = _PausedRunner()
        calls = []
        original = AgentRunService.claim_for_execution

        async def counted(service, run_id):
            calls.append(run_id)
            return await original(service, run_id)

        monkeypatch.setattr(AgentRunService, "claim_for_execution", counted)
        app = _app(case, lambda *_: runner)
        other = uuid4()
        async with case.factory.begin() as session:
            session.add(AgentRunModel(id=other, task_id=case.task_id, attempt_number=2, status="queued"))
        try:
            async with _client(app) as client:
                one, two = await asyncio.gather(*[_command(client, case.run_id, "start") for _ in range(2)])
                accepted, refused = sorted((one, two), key=lambda response: response.status_code)
                assert accepted.status_code == 202
                _assert_error(refused, 409, "RUN_ALREADY_STARTED")
                await runner.entered.wait()
                _assert_error(await _command(client, other, "start"), 429, "RUN_CAPACITY_EXCEEDED")
                assert calls == [case.run_id]
                async with case.factory() as session:
                    assert (await session.get(AgentRunModel, other)).status == "queued"
                task = _execution(app)
                assert (await _command(client, case.run_id, "cancel")).status_code == 200
                await _wait_for_cleanup(app, task)
                assert runner.cancelled.is_set()
        finally:
            async with case.factory.begin() as session:
                await session.execute(delete(AgentRunModel).where(AgentRunModel.id == other))
    run_scenario(check)


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_http_start_rejects_non_queued_and_cancel_rejects_terminal(run_scenario, status):
    async def check(case):
        async with case.factory.begin() as session:
            run = await session.get(AgentRunModel, case.run_id)
            run.status = status
        app = _app(case, lambda *_: pytest.fail("Runner must not be composed"))
        async with _client(app) as client:
            _assert_error(await _command(client, case.run_id, "start"), 409, "RUN_ALREADY_STARTED")
            if status != "running":
                _assert_error(await _command(client, case.run_id, "cancel"), 409, "RUN_NOT_CANCELLABLE")
            for command in ("start", "cancel"):
                _assert_error(await _command(client, uuid4(), command), 404, "AGENT_RUN_NOT_FOUND")
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == (status, "queued") and not events
    run_scenario(check)


def test_http_foreign_workspace_binding_rejects_start_and_cancel_before_composition(run_scenario):
    async def check(case):
        app = _app(case, lambda *_: pytest.fail("Wrong workspace reached composition"), workspace_id=uuid4())
        async with _client(app) as client:
            for command in ("start", "cancel"):
                _assert_error(await _command(client, case.run_id, command), 503, "RUN_EXECUTION_UNAVAILABLE")
        run, _, events = await _snapshot(case.factory, case)
        assert run.status == "queued" and not events
    run_scenario(check)


def test_http_cancel_queued_and_replay_persist_one_transition_without_composing_runner(run_scenario):
    async def check(case):
        app = _app(case, lambda *_: pytest.fail("Cancel must not compose a runner"))
        async with _client(app) as client:
            first = await _command(client, case.run_id, "cancel")
            repeat = await _command(client, case.run_id, "cancel")
            assert first.status_code == repeat.status_code == 200 and first.json() == repeat.json()
            assert first.json()["status"] == "cancelled"
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("cancelled", "queued")
        assert _transitions(events) == [("queued", "cancelled")]
    run_scenario(check)


def test_http_cancel_before_claim_blocks_runner_and_keeps_terminal_status(run_scenario, monkeypatch):
    async def check(case):
        entered, release = asyncio.Event(), asyncio.Event()
        original = AgentService._claim

        async def paused(service, run_id):
            entered.set()
            await release.wait()
            return await original(service, run_id)

        class UnreachableRunner:
            async def run(self, request):
                pytest.fail("A cancelled run must not reach the runner")

        monkeypatch.setattr(AgentService, "_claim", paused)
        app = _app(case, lambda *_: UnreachableRunner())
        async with _client(app) as client:
            assert (await _command(client, case.run_id, "start")).status_code == 202
            await entered.wait()
            task = _execution(app)
            try:
                cancelled = await _command(client, case.run_id, "cancel")
                assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                run, _, events = await _snapshot(case.factory, case)
                assert run.status == "cancelled" and _transitions(events) == [("queued", "cancelled")]
            finally:
                release.set()
            await _wait_for_cleanup(app, task)
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status, run.final_result) == ("cancelled", "queued", None)
        assert _transitions(events) == [("queued", "cancelled")]
    run_scenario(check)


def test_http_cancel_signals_running_coroutine_after_commit_and_discards_late_result(run_scenario):
    async def check(case):
        signaled = asyncio.Event()
        runner = _PausedRunner()

        class ReturningAfterCancel:
            async def run(self, request):
                try:
                    return await runner.run(request)
                except asyncio.CancelledError:
                    run, _, events = await _snapshot(case.factory, case)
                    assert run.status == "cancelled" and _transitions(events)[-1] == ("running", "cancelled")
                    signaled.set()
                    return FINAL

        app = _app(case, lambda *_: ReturningAfterCancel())
        async with _client(app) as client:
            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
            assert (await _command(client, case.run_id, "cancel")).json()["status"] == "cancelled"
            await _wait_for_cleanup(app, task)
            assert signaled.is_set()
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status, run.final_result) == ("cancelled", "queued", None)
        assert _transitions(events) == [("queued", "running"), ("running", "cancelled")]
    run_scenario(check)


def test_http_request_disconnect_after_response_start_preserves_background_execution(run_scenario):
    async def check(case):
        runner = _PausedRunner()
        headers_sent = asyncio.Event()
        app = _app(case, lambda *_: runner)

        async def interrupted_transport(scope, receive, send):
            async def interrupted_send(message):
                if message["type"] == "http.response.start":
                    assert message["status"] == 202
                    await send(message)
                    headers_sent.set()
                    await asyncio.Event().wait()
                else:
                    await send(message)
            await app(scope, receive, interrupted_send)

        async with _client(app, transport_app=interrupted_transport) as client:
            request = asyncio.create_task(_command(client, case.run_id, "start"))
            try:
                await headers_sent.wait()
                await runner.entered.wait()
                task = _execution(app)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert not task.done() and not runner.cancelled.is_set()
                runner.release.set()
                await _wait_for_cleanup(app, task)
            finally:
                runner.release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("verifying", "queued")
        assert _transitions(events) == [("queued", "running"), ("running", "verifying")]
    run_scenario(check)


@pytest.mark.parametrize("db_failure", [False, True], ids=["persisted-cancel", "database-unavailable"])
def test_http_lifespan_shutdown_observes_local_execution(run_scenario, monkeypatch, caplog, db_failure):
    async def check(case):
        runner = _PausedRunner()
        app = _app(case, lambda *_: runner)
        if db_failure:
            async def fail_cancellation(*args):
                raise RuntimeError("private database fault")

            monkeypatch.setattr(AgentRunService, "cancel_execution", fail_cancellation)
        async with _client(app) as client:
            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
        await _wait_for_cleanup(app, task)
        assert runner.cancelled.is_set()
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("running" if db_failure else "cancelled", "queued")
        assert ("http_agent_run_shutdown_cancel_unconfirmed" in caplog.text) is db_failure
        assert "private database fault" not in caplog.text
        assert _transitions(events) == (
            [("queued", "running")] if db_failure else [("queued", "running"), ("running", "cancelled")]
        )
    run_scenario(check)


def test_http_background_runner_failure_is_observed_and_logs_no_payload(run_scenario, caplog):
    async def check(case):
        entered, release = asyncio.Event(), asyncio.Event()

        class FailingRunner:
            async def run(self, request):
                entered.set()
                await release.wait()
                raise RuntimeError("synthetic private /host/path and key")

        app = _app(case, lambda *_: FailingRunner())
        async with _client(app) as client:
            response = await _command(client, case.run_id, "start")
            assert response.status_code == 202
            await entered.wait()
            task = _execution(app)
            release.set()
            await _wait_for_cleanup(app, task)
        run, parent, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("failed", "queued")
        assert _transitions(events)[-1] == ("running", "failed")
        assert "http_agent_run_execution_failed" in caplog.text
        for data in (response.text, caplog.text, json.dumps([item.payload for item in events])):
            assert "/host/path" not in data and "synthetic private" not in data
    run_scenario(check)


def test_http_cancel_constraint_failure_rolls_back_without_signaling_runner(run_scenario, monkeypatch):
    async def check(case):
        runner = _PausedRunner()
        app = _app(case, lambda *_: runner)
        original = AgentEventRepository.add_status_changed

        async def invalid_event(repository, run_id, old, new):
            event = await original(repository, run_id, old, new)
            if new is AgentRunStatus.CANCELLED:
                event.sequence = 0
            return event

        async with _client(app) as client:
            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
            with monkeypatch.context() as fail:
                fail.setattr(AgentEventRepository, "add_status_changed", invalid_event)
                response = await _command(client, case.run_id, "cancel")
                assert response.status_code == 500 and "agent_events" not in response.text
            run, _, events = await _snapshot(case.factory, case)
            assert run.status == "running" and _transitions(events) == [("queued", "running")]
            assert not runner.cancelled.is_set() and not task.done()
            assert (await _command(client, case.run_id, "cancel")).status_code == 200
            await _wait_for_cleanup(app, task)
    run_scenario(check)


def test_http_composition_failure_leaves_no_reservation_and_sanitizes_error(run_scenario, caplog):
    async def check(case):
        runner = _PausedRunner()
        attempts = []

        def build(factory, run_id):
            attempts.append(run_id)
            if len(attempts) == 1:
                raise RuntimeError("private runner credential /host/root")
            return runner

        app = _app(case, build)
        async with _client(app) as client:
            malformed = await _command(client, case.run_id, "start", json={"fixture_root": "/host/root"})
            _assert_error(malformed, 422, "VALIDATION_ERROR")
            assert not attempts
            refused = await _command(client, case.run_id, "start")
            _assert_error(refused, 503, "RUN_EXECUTION_UNAVAILABLE")
            assert "private runner credential" not in refused.text + caplog.text
            assert app.state.run_coordinator._execution is None
            run, _, events = await _snapshot(case.factory, case)
            assert run.status == "queued" and not events

            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
            assert (await _command(client, case.run_id, "cancel")).status_code == 200
            await _wait_for_cleanup(app, task)
            assert attempts == [case.run_id, case.run_id]
    run_scenario(check)


def test_http_lifespan_shutdown_times_out_unconfirmed_cancel_and_cleans_runner(run_scenario, monkeypatch, caplog):
    async def check(case):
        runner = _PausedRunner()
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def stalled_cancel(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(AgentRunService, "cancel_execution", stalled_cancel)
        monkeypatch.setattr(in_process_run_coordinator, "_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
        app = _app(case, lambda *_: runner)
        async with _client(app) as client:
            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
        await _wait_for_cleanup(app, task)
        assert entered.is_set() and cleaned.is_set() and runner.cancelled.is_set()
        assert app.state.run_coordinator._shutdown_cancel is None
        run, _, events = await _snapshot(case.factory, case)
        assert run.status == "running" and _transitions(events) == [("queued", "running")]
        assert "http_agent_run_shutdown_cancel_unconfirmed" in caplog.text
    run_scenario(check)
