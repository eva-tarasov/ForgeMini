"""Lifespan-owned AgentRun HTTP execution on the dedicated PostgreSQL fixture."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete

from app.agent_runtime.structured_output import TaskAnalysis
from app.db.models import AgentRunModel
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.permissions.context import PathScope
from app.repositories.agent_events import AgentEventRepository
from app.services import in_process_run_coordinator, verification_service
from app.services.agent_result import AgentFinalResult
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError
from app.services.agent_service import AgentService
from app.services.approved_repository_agent_runner import ApprovedRepositoryAgentRunner, ApprovedRepositoryFixtureBinding
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.pending_approvals import PendingApprovals
from app.services.repository_agent_runner import RepositoryAgentRunner
from app.services.verification_contract import FileDigestCheck, VerificationBinding, VerificationPlan, fingerprint_criteria
from app.services.verification_evidence import VERIFICATION_RECORDED
from app.services.verification_observer import TrustedFixtureAccess
from test_approved_repository_integration import ORIGINAL, WRITE_ID, _approval_state, _fixture_patch, _tool_reply
from test_repository_agent_integration import (
    API_KEY, CALL_ID, CONTENT, CRITERIA, FILE, _offline_model, _read_reply, _snapshot, database,
)


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="set FORGE_REPOSITORY_AGENT_TEST_DB and POSTGRES_DB to the dedicated fixture DB",
)
FINAL = AgentFinalResult(
    summary="Synthetic preliminary result", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)


def _app(case, build_runner, *, workspace_id=None):
    return create_app(
        session_factory=case.factory,
        execution_binding=RunExecutionBinding(workspace_id or case.binding.workspace_id, build_runner),
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


def test_http_start_accepts_before_claim_and_real_offline_graph_reaches_verifying(database, monkeypatch, caplog):
    async def check(case, idle):
        preclaim, release = asyncio.Event(), asyncio.Event()
        claim = AgentService._claim
        claims = []

        async def paused(service, run_id):
            claims.append(run_id)
            preclaim.set()
            await release.wait()
            return await claim(service, run_id)

        monkeypatch.setattr(AgentService, "_claim", paused)
        analysis = TaskAnalysis(summary="Offline fixture checked", relevant_components=["repository"],
                                assumptions=[], blocked_reason=None)

        async def reply(index, payload):
            idle()
            if index == 0:
                return _read_reply()
            if index == 1:
                assert payload["messages"][-1]["tool_call_id"] == CALL_ID
                return "stop", {"content": "fixture inspected"}
            assert index == 2
            return "stop", {"content": analysis.model_dump_json()}

        caplog.set_level(logging.INFO)
        async with _offline_model(reply) as (model, calls):
            app = _app(case, lambda factory, _run_id: RepositoryAgentRunner(factory, model, case.binding))
            async with _client(app) as client:
                start = await _command(client, case.run_id, "start")
                assert start.status_code == 202
                assert start.json() == {"run_id": str(case.run_id), "accepted": True}
                assert start.headers["Location"] == f"/agent-runs/{case.run_id}"
                await preclaim.wait()
                task = _execution(app)
                try:
                    waiting, _, tools, events = await _snapshot(case.factory, case)
                    assert waiting.status == "queued" and not tools and not events and not calls
                    assert (await client.get(start.headers["Location"])).json()["status"] == "queued"
                finally:
                    release.set()
                await _wait_for_cleanup(app, task)
                assert len(calls) == 3 and claims == [case.run_id]
                run, parent, (tool,), events = await _snapshot(case.factory, case)
                assert (run.status, parent.status, tool.status) == ("verifying", "queued", "succeeded")
                assert _transitions(events) == [("queued", "running"), ("running", "verifying")]
                assert run.final_result["result"]["summary"] == analysis.summary
                result = await client.get(start.headers["Location"])
                for private in (API_KEY, str(case.binding.root), CONTENT, FILE, CALL_ID):
                    assert private not in start.text + result.text + caplog.text
    database(check)


def test_http_concurrent_start_conflicts_and_capacity_allows_one_claim(database, monkeypatch):
    async def check(case, _idle):
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
    database(check)


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_http_start_rejects_non_queued_and_cancel_rejects_terminal(database, status):
    async def check(case, _idle):
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
        run, parent, calls, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == (status, "queued") and not calls and not events
    database(check)


def test_http_foreign_workspace_binding_rejects_start_and_cancel_before_composition(database):
    async def check(case, _idle):
        app = _app(case, lambda *_: pytest.fail("Wrong workspace reached composition"), workspace_id=uuid4())
        async with _client(app) as client:
            for command in ("start", "cancel"):
                _assert_error(await _command(client, case.run_id, command), 503, "RUN_EXECUTION_UNAVAILABLE")
        run, _, _, events = await _snapshot(case.factory, case)
        assert run.status == "queued" and not events
    database(check)


def test_http_cancel_queued_and_replay_persist_one_transition_without_composing_runner(database):
    async def check(case, _idle):
        app = _app(case, lambda *_: pytest.fail("Cancel must not compose a runner"))
        async with _client(app) as client:
            first = await _command(client, case.run_id, "cancel")
            repeat = await _command(client, case.run_id, "cancel")
            assert first.status_code == repeat.status_code == 200 and first.json() == repeat.json()
            assert first.json()["status"] == "cancelled"
        run, parent, calls, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("cancelled", "queued") and not calls
        assert _transitions(events) == [("queued", "cancelled")]
    database(check)


def test_http_cancel_before_claim_blocks_runner_and_keeps_terminal_status(database, monkeypatch):
    async def check(case, _idle):
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
                run, _, _, events = await _snapshot(case.factory, case)
                assert run.status == "cancelled" and _transitions(events) == [("queued", "cancelled")]
            finally:
                release.set()
            await _wait_for_cleanup(app, task)
        run, parent, calls, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status, run.final_result) == ("cancelled", "queued", None) and not calls
        assert _transitions(events) == [("queued", "cancelled")]
    database(check)


def test_http_cancel_signals_running_coroutine_after_commit_and_discards_late_result(database):
    async def check(case, _idle):
        signaled = asyncio.Event()
        runner = _PausedRunner()

        class ReturningAfterCancel:
            async def run(self, request):
                try:
                    return await runner.run(request)
                except asyncio.CancelledError:
                    run, _, _, events = await _snapshot(case.factory, case)
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
        run, parent, calls, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status, run.final_result) == ("cancelled", "queued", None) and not calls
        assert _transitions(events) == [("queued", "running"), ("running", "cancelled")]
    database(check)


def test_http_cancel_waiting_approval_keeps_fixture_unchanged(database, monkeypatch):
    async def check(case, idle):
        published = asyncio.Event()
        original = PendingApprovals.publish

        def observed(pending, operation):
            original(pending, operation)
            published.set()

        monkeypatch.setattr(PendingApprovals, "publish", observed)
        patch = _fixture_patch(case)
        binding = ApprovedRepositoryFixtureBinding(
            case.binding.workspace_id, case.binding.root, case.binding.baseline,
            frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE}),
            (PathScope(FILE),), (PathScope(FILE),),
        )

        async def reply(index, _payload):
            assert index == 0
            idle()
            return _tool_reply("apply_patch", WRITE_ID, patch=patch)

        async with _offline_model(reply) as (model, calls):
            app = _app(case, lambda factory, run_id: ApprovedRepositoryAgentRunner(
                factory, model, binding, run_id=run_id,
            ))
            async with _client(app) as client:
                assert (await _command(client, case.run_id, "start")).status_code == 202
                await published.wait()
                task = _execution(app)
                run, _, (tool,), _, (approval,) = await _approval_state(case.factory, case)
                assert (run.status, tool.status, approval.status) == ("waiting_approval", "proposed", "pending")
                response = await _command(client, case.run_id, "cancel")
                assert response.status_code == 200 and response.json()["status"] == "cancelled"
                await _wait_for_cleanup(app, task)
            assert len(calls) == 1
        run, parent, (tool,), events, (approval,) = await _approval_state(case.factory, case)
        assert (run.status, parent.status, tool.status, approval.status) == ("cancelled", "queued", "failed", "rejected")
        assert (case.binding.root / FILE).read_text() == ORIGINAL
        assert not any(event.event_type == "tool_call_started" for event in events)
        assert _transitions(events)[-1] == ("waiting_approval", "cancelled")
    database(check)


def test_http_cancel_verifying_run_wins_before_verification_publication(database, monkeypatch):
    async def check(case, _idle):
        observed, release = asyncio.Event(), asyncio.Event()
        original = verification_service.observe

        async def paused_observation(plan, access):
            result = await original(plan, access)
            observed.set()
            await release.wait()
            return result

        monkeypatch.setattr(verification_service, "observe", paused_observation)
        async with case.factory() as session:
            runs = AgentRunService(session)
            await runs.claim_for_execution(case.run_id)
            await runs.complete_execution(case.run_id, FINAL)
        binding = VerificationBinding(case.run_id, case.task_id, case.binding.workspace_id, case.binding.baseline.revision)
        plan = VerificationPlan(
            uuid4(), binding, fingerprint_criteria(CRITERIA),
            (FileDigestCheck(uuid4(), 0, FILE, sha256(CONTENT.encode()).hexdigest()),),
        )
        access = TrustedFixtureAccess(
            plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=binding,
            root=case.binding.root, read_scopes=(PathScope(FILE),), acceptance_criteria=CRITERIA,
        )
        app = _app(case, lambda *_: pytest.fail("Cancel must not compose a runner"))
        async with _client(app) as client:
            pending = asyncio.create_task(verification_service.VerificationService(case.factory).verify(
                plan, access, attempt_id=uuid4(), isolated_fixture=True,
            ))
            try:
                await observed.wait()
                response = await _command(client, case.run_id, "cancel")
                assert response.status_code == 200 and response.json()["status"] == "cancelled"
                release.set()
                with pytest.raises(StaleAgentRunStatusError):
                    await pending
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        run, parent, _, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("cancelled", "queued")
        assert _transitions(events)[-1] == ("verifying", "cancelled")
        assert not any(event.event_type == VERIFICATION_RECORDED for event in events)
    database(check)


def test_http_request_disconnect_after_response_start_preserves_background_execution(database):
    async def check(case, _idle):
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
        run, parent, _, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("verifying", "queued")
        assert _transitions(events) == [("queued", "running"), ("running", "verifying")]
    database(check)


@pytest.mark.parametrize("db_failure", [False, True], ids=["persisted-cancel", "database-unavailable"])
def test_http_lifespan_shutdown_observes_local_execution(database, monkeypatch, caplog, db_failure):
    async def check(case, _idle):
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
        run, parent, _, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("running" if db_failure else "cancelled", "queued")
        assert ("http_agent_run_shutdown_cancel_unconfirmed" in caplog.text) is db_failure
        assert "private database fault" not in caplog.text
        assert _transitions(events) == (
            [("queued", "running")] if db_failure else [("queued", "running"), ("running", "cancelled")]
        )
    database(check)


def test_http_background_runner_failure_is_observed_and_logs_no_payload(database, caplog):
    async def check(case, _idle):
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
        run, parent, _, events = await _snapshot(case.factory, case)
        assert (run.status, parent.status) == ("failed", "queued")
        assert _transitions(events)[-1] == ("running", "failed")
        assert "http_agent_run_execution_failed" in caplog.text
        for data in (response.text, caplog.text, json.dumps([item.payload for item in events])):
            assert "/host/path" not in data and "synthetic private" not in data
    database(check)


def test_http_cancel_constraint_failure_rolls_back_without_signaling_runner(database, monkeypatch):
    async def check(case, _idle):
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
            run, _, _, events = await _snapshot(case.factory, case)
            assert run.status == "running" and _transitions(events) == [("queued", "running")]
            assert not runner.cancelled.is_set() and not task.done()
            assert (await _command(client, case.run_id, "cancel")).status_code == 200
            await _wait_for_cleanup(app, task)
    database(check)


def test_http_composition_failure_leaves_no_reservation_and_sanitizes_error(database, caplog):
    async def check(case, _idle):
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
            run, _, _, events = await _snapshot(case.factory, case)
            assert run.status == "queued" and not events

            assert (await _command(client, case.run_id, "start")).status_code == 202
            await runner.entered.wait()
            task = _execution(app)
            assert (await _command(client, case.run_id, "cancel")).status_code == 200
            await _wait_for_cleanup(app, task)
            assert attempts == [case.run_id, case.run_id]
    database(check)


def test_http_lifespan_shutdown_times_out_unconfirmed_cancel_and_cleans_runner(database, monkeypatch, caplog):
    async def check(case, _idle):
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
        run, _, _, events = await _snapshot(case.factory, case)
        assert run.status == "running" and _transitions(events) == [("queued", "running")]
        assert "http_agent_run_shutdown_cancel_unconfirmed" in caplog.text
    database(check)
