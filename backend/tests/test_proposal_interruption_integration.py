"""Регрессии на границе PostgreSQL commit и возврата proposal."""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_approved_repository_integration import (
    FILE,
    ORIGINAL,
    _approval_state,
    _fixture_patch,
    _patch_reply,
    _run_agent,
    _TimeoutProbe,
    _tool_reply,
)
from test_repository_agent_integration import _offline_model, database

from app.db.session import create_engine, create_session_factory
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.approvals import ApprovalRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services import agent_service as agent_service_module
from app.services.agent_runner import AgentRunnerError
from app.services.approval_gate import ApprovalGate
from app.services.approval_outcome import ApprovalOutcome, ApprovalOutcomeHolder
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import ToolCallService
from app.tools.executor import ToolExecutor


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="Нужны FORGE_REPOSITORY_AGENT_TEST_DB и POSTGRES_DB для регрессий R1A",
)


async def _read_reply(index, _payload):
    assert index == 0
    return _tool_reply("read_file", "read-interrupted-proposal", path=FILE)


class _PauseProposal:
    def __init__(self, monkeypatch, kind, *, before_commit=False):
        self.reached = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.owner = None
        self.target_session = None
        self.payload = None
        self.original_cancellation = None
        self.rolled_back = False
        event = "approval_requested" if kind == "write" else "tool_call_proposed"
        original_append = AgentEventRepository.append
        original_commit, original_close, original_rollback = AsyncSession.commit, AsyncSession.close, AsyncSession.rollback

        async def append(repository, run_id, *, event_type, payload):
            row = await original_append(repository, run_id, event_type=event_type, payload=payload)
            if event_type == event and self.payload is None:
                self.target_session = repository.session
                self.payload = payload
            return row

        async def commit(session):
            if before_commit and session is self.target_session:
                # Записи видит текущая транзакция, но ещё не видит независимая сессия.
                await session.flush()
                await self._pause()
            await original_commit(session)

        async def close(session):
            await original_close(session)
            if not before_commit and session is self.target_session:
                # Commit и настоящий close уже завершились, их caller ещё не продолжился.
                await self._pause()

        async def rollback(session):
            await original_rollback(session)
            if session is self.target_session:
                self.rolled_back = True

        monkeypatch.setattr(AgentEventRepository, "append", append)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        monkeypatch.setattr(AsyncSession, "close", close)
        monkeypatch.setattr(AsyncSession, "rollback", rollback)

    async def _pause(self):
        self.owner = asyncio.current_task()
        self.reached.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.original_cancellation = error
            self.cancelled.set()
            raise


def _observe_slots(monkeypatch):
    slots = []
    original = PendingApprovals.reserve

    def reserve(registry, operation):
        original(registry, operation)
        slots.append((registry, operation))

    monkeypatch.setattr(PendingApprovals, "reserve", reserve)
    return slots


def _forbid_handler(monkeypatch):
    async def invoke(*args, **kwargs):
        pytest.fail("Handler не должен запускаться до завершения proposal")

    monkeypatch.setattr(ToolExecutor, "_invoke_handler", invoke)


def _expect_abort(state, *, kind, deadline, confirmed=True, audit_error=False):
    run, task, calls, events, approvals = state
    expected = "failed" if audit_error or (deadline and (kind == "read" or not confirmed)) else "cancelled"
    code = "audit_persistence_failed" if audit_error else "cancelled"
    failure_code = "graph_execution_failed" if audit_error else "execution_deadline_exceeded"
    finished = [event for event in events if event.event_type == "tool_call_finished"]
    failures = [event for event in events if event.event_type == "agent_execution_failed"]
    assert task.status == "queued" and run.final_result is None
    assert not any(event.event_type in ("tool_call_started", "agent_result_recorded") for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert (run.status, [call.status for call in calls], [approval.status for approval in approvals], len(finished)) == (
        expected, ["failed"] if confirmed else [], ["rejected"] if confirmed and kind == "write" else [],
        int(confirmed),
    )
    assert [event.payload["code"] for event in failures] == ([failure_code] if expected == "failed" else [])
    if confirmed:
        (call,), (event,) = calls, finished
        assert event.payload == {
            "schema_version": 1, "tool_call_id": str(call.id), "tool_name": call.tool_name,
            "tool_version": 1, "status": "failed", "error_code": code,
        }
    decisions = [event.payload for event in events if event.event_type == "approval_decided"]
    if confirmed and kind == "write":
        (approval,), (decision,) = approvals, decisions
        assert approval.decided_at is not None and approval.tool_call_id == call.id
        assert decision == {
            "schema_version": 1, "approval_id": str(approval.id), "tool_call_id": str(call.id),
            "decision": "rejected", "error_code": code,
        }
    else:
        assert not decisions


@pytest.mark.parametrize("kind", ["read", "write"])
@pytest.mark.parametrize("cause", ["cancel", "deadline"])
def test_postgres_interrupted_committed_proposal_is_finalized(database, monkeypatch, kind, cause):
    async def check(case, idle):
        patch = _fixture_patch(case)
        reply = _patch_reply(patch, idle) if kind == "write" else _read_reply
        pause = _PauseProposal(monkeypatch, kind)
        slots = _observe_slots(monkeypatch)
        _forbid_handler(monkeypatch)
        timer = _TimeoutProbe()
        monkeypatch.setattr(agent_service_module, "asyncio", timer)
        emissions = []
        publish = ApprovalOutcomeHolder._publish
        reconcile = ApprovalGate._reconcile_unpublished_request

        def observe_publication(holder, outcome):
            accepted = publish(holder, outcome)
            emissions.append((outcome, accepted))
            return accepted

        async def observe_cleanup(gate, operation, code):
            assert emissions == [(ApprovalOutcome(operation.reference, code, interrupted_request=True), True)]
            return await reconcile(gate, operation, code)

        monkeypatch.setattr(ApprovalOutcomeHolder, "_publish", observe_publication)
        monkeypatch.setattr(ApprovalGate, "_reconcile_unpublished_request", observe_cleanup)
        independent = create_engine(case.settings)
        try:
            fresh = create_session_factory(independent)
            async with _offline_model(reply) as (model, responses):
                async with _run_agent(case, model, monkeypatch) as (service, runner, execution, published):
                    await asyncio.wait_for(pause.reached.wait(), 3)
                    run, _, (call,), events, approvals = await _approval_state(fresh, case)
                    assert call.status == "proposed" and str(call.id) == pause.payload["tool_call_id"]
                    assert any(event.event_type == "tool_call_proposed" for event in events)
                    assert not published.is_set() and runner._approval_preview() is None
                    if kind == "write":
                        assert run.status == "waiting_approval"
                        (approval,), ((registry, operation),) = approvals, slots
                        assert str(approval.id) == pause.payload["approval_id"] and approval.status == "pending"
                        assert approval.tool_call_id == call.id
                        registry.require_reserved(operation)
                    else:
                        assert run.status == "running" and not approvals and not slots
                    if cause == "deadline":
                        await timer.expire()
                        with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                            await asyncio.wait_for(execution, 5)
                    else:
                        await service.cancel_run(case.run_id)
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(execution, 5)
                    assert pause.cancelled.is_set()
                    assert not published.is_set()
                    assert all(not registry._entries for registry, _ in slots)
                    assert len(emissions) == (1 if kind == "write" else 0)
            assert len(responses) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
            _expect_abort(await _approval_state(fresh, case), kind=kind, deadline=cause == "deadline")
        finally:
            await independent.dispose()

    database(check)


@pytest.mark.parametrize("kind", ["read", "write"])
def test_postgres_proposal_interrupted_before_commit_rolls_back(database, monkeypatch, kind):
    async def check(case, idle):
        patch = _fixture_patch(case)
        reply = _patch_reply(patch, idle) if kind == "write" else _read_reply
        pause = _PauseProposal(monkeypatch, kind, before_commit=True)
        slots = _observe_slots(monkeypatch)
        _forbid_handler(monkeypatch)
        timer = _TimeoutProbe()
        monkeypatch.setattr(agent_service_module, "asyncio", timer)
        independent = create_engine(case.settings)
        try:
            fresh = create_session_factory(independent)
            async with _offline_model(reply) as (model, responses):
                async with _run_agent(case, model, monkeypatch) as (_, runner, execution, published):
                    await asyncio.wait_for(pause.reached.wait(), 3)
                    run, _, calls, events, approvals = await _approval_state(fresh, case)
                    assert run.status == "running" and not calls and not approvals
                    assert not any(event.event_type == "tool_call_proposed" for event in events)
                    assert runner._approval_preview() is None and not published.is_set()
                    await timer.expire()
                    with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                        await asyncio.wait_for(execution, 5)
                    assert pause.rolled_back and not published.is_set()
                    assert all(not registry._entries for registry, _ in slots)
            assert len(responses) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
            state = await _approval_state(fresh, case)
            assert not any(event.event_type.startswith(("tool_call_", "approval_")) for event in state[3])
            _expect_abort(state, kind=kind, deadline=True, confirmed=False)
        finally:
            await independent.dispose()

    database(check)


@pytest.mark.parametrize("kind", ["read", "write"])
def test_postgres_concurrent_cancel_and_repeated_cleanup_finalize_once(database, monkeypatch, kind):
    async def check(case, idle):
        patch = _fixture_patch(case)
        reply = _patch_reply(patch, idle) if kind == "write" else _read_reply
        pause = _PauseProposal(monkeypatch, kind)
        slots = _observe_slots(monkeypatch)
        _forbid_handler(monkeypatch)
        cancel_tasks = []
        pids = []
        lock_order = []
        both_entered = asyncio.Event()
        reconciliations = []

        async def track_run(repo, run_id):
            task = asyncio.current_task()
            if task in cancel_tasks:
                pids.append(await repo.session.scalar(text("SELECT pg_backend_pid()")))
                if len(pids) == 2:
                    both_entered.set()
            if task is pause.owner:
                lock_order.append("run")
            return await get_run(repo, run_id)

        def track_lock(original, name):
            async def lock(*args):
                if asyncio.current_task() is pause.owner:
                    lock_order.append(name)
                return await original(*args)
            return lock

        async def reconcile_twice(service, identity, code):
            for _ in range(2):
                await reconcile(service, identity, code)
                reconciliations.append(identity)

        get_run = AgentRunRepository.get_for_update
        monkeypatch.setattr(AgentRunRepository, "get_for_update", track_run)
        monkeypatch.setattr(ToolCallRepository, "get_for_update", track_lock(ToolCallRepository.get_for_update, "tool"))
        monkeypatch.setattr(ApprovalRepository, "get_for_update", track_lock(ApprovalRepository.get_for_update, "approval"))
        owner = ApprovalGate if kind == "write" else ToolCallService
        method = "_reconcile_unpublished_request" if kind == "write" else "_reconcile_proposal"
        reconcile = getattr(owner, method)
        monkeypatch.setattr(owner, method, reconcile_twice)

        independent = create_engine(case.settings)
        try:
            fresh = create_session_factory(independent)
            async with _offline_model(reply) as (model, responses):
                async with _run_agent(case, model, monkeypatch) as (service, runner, execution, published):
                    await asyncio.wait_for(pause.reached.wait(), 3)
                    try:
                        async with fresh.begin() as blocker:
                            await get_run(AgentRunRepository(blocker), case.run_id)
                            pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                            cancel_tasks.extend(asyncio.create_task(service.cancel_run(case.run_id)) for _ in range(2))
                            await asyncio.wait_for(both_entered.wait(), 3)
                            async with fresh() as observer, asyncio.timeout(3):
                                query = text("SELECT pg_blocking_pids(:pid)")
                                while True:
                                    blockers = [await observer.scalar(query, {"pid": waiter}) for waiter in pids]
                                    if all(blockers) and any(pid in waiting for waiting in blockers):
                                        break
                                    await asyncio.sleep(0.01)
                        await asyncio.wait_for(asyncio.gather(*cancel_tasks), 5)
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(execution, 5)
                    finally:
                        for task in cancel_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*cancel_tasks, return_exceptions=True)
                    assert runner._approval_preview() is None and not published.is_set()
                    assert all(not registry._entries for registry, _ in slots)
            assert len(reconciliations) == 2 and reconciliations[0] is reconciliations[1]
            assert lock_order == (["run", "tool", "approval", "run", "tool"] if kind == "write" else
                                  ["run", "tool", "run", "tool"])
            state = await _approval_state(fresh, case)
            cancelled = [event for event in state[3] if event.event_type == "agent_run_status_changed"
                         and event.payload["to"] == "cancelled"]
            assert len(cancelled) == 1
            assert len(responses) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
            _expect_abort(state, kind=kind, deadline=False)
        finally:
            await independent.dispose()

    database(check)


@pytest.mark.parametrize("failure", ["db-error", "repeat-cancel", "cleanup-timeout"])
def test_postgres_cleanup_failure_keeps_original_deadline_and_drops_slot(database, monkeypatch, failure):
    async def check(case, idle):
        patch = _fixture_patch(case)
        pause = _PauseProposal(monkeypatch, "write")
        slots = _observe_slots(monkeypatch)
        timer = _TimeoutProbe()
        entered, exited = asyncio.Event(), asyncio.Event()
        _forbid_handler(monkeypatch)
        monkeypatch.setattr(agent_service_module, "asyncio", timer)

        async def fail_cleanup(*args):
            entered.set()
            try:
                if failure == "db-error":
                    raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED)
                await asyncio.Event().wait()
            finally:
                exited.set()

        monkeypatch.setattr(ApprovalGate, "_reconcile_unpublished_request", fail_cleanup)
        async with _offline_model(_patch_reply(patch, idle)) as (model, responses):
            async with _run_agent(case, model, monkeypatch) as (_, runner, execution, published):
                await asyncio.wait_for(pause.reached.wait(), 3)
                await timer.expire()
                await asyncio.wait_for(entered.wait(), 3)
                if failure == "repeat-cancel":
                    pause.owner.cancel("повторная отмена в cleanup")
                with pytest.raises(AgentRunnerError) as caught:
                    await asyncio.wait_for(execution, 5)
                assert caught.value.reason == "execution_deadline_exceeded"
                assert caught.value.__notes__ == ["agent_failure_persistence_failed"]
                assert pause.original_cancellation.__notes__ == ["tool_call_audit_finalization_failed"]
                assert exited.is_set() and pause.owner.done()
                assert not published.is_set() and runner._approval_preview() is None
                ((registry, _),) = slots
                assert not registry._entries
        run, _, (call,), events, (approval,) = await _approval_state(case.factory, case)
        assert (run.status, call.status, approval.status) == ("waiting_approval", "proposed", "pending")
        assert not any(event.event_type in (
            "tool_call_started", "tool_call_finished", "approval_decided", "agent_execution_failed",
        ) for event in events)
        assert len(responses) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL

    database(check)


@pytest.mark.parametrize("kind", ["read", "write"])
def test_postgres_lost_commit_acknowledgment_is_reconciled(database, monkeypatch, kind):
    async def check(case, idle):
        patch = _fixture_patch(case)
        reply = _patch_reply(patch, idle) if kind == "write" else _read_reply
        event_name = "approval_requested" if kind == "write" else "tool_call_proposed"
        original_append, original_commit = AgentEventRepository.append, AsyncSession.commit
        targeted_session = None
        lost = asyncio.Event()
        _forbid_handler(monkeypatch)

        async def mark_proposal(repo, run_id, *, event_type, payload):
            nonlocal targeted_session
            row = await original_append(repo, run_id, event_type=event_type, payload=payload)
            if event_type == event_name:
                targeted_session = repo.session
            return row

        async def lose_acknowledgment(session):
            await original_commit(session)
            if session is targeted_session:
                lost.set()
                raise OSError("synthetic-database-acknowledgment-error")

        monkeypatch.setattr(AgentEventRepository, "append", mark_proposal)
        monkeypatch.setattr(AsyncSession, "commit", lose_acknowledgment)
        async with _offline_model(reply) as (model, responses):
            async with _run_agent(case, model, monkeypatch) as (_, runner, execution, published):
                with pytest.raises(AgentRunnerError, match="^graph_execution_failed$") as caught:
                    await asyncio.wait_for(execution, 5)
                assert caught.value.__cause__.args == ("audit_persistence_failed",)
                assert lost.is_set() and not published.is_set() and runner._approval_preview() is None
        assert len(responses) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
        _expect_abort(await _approval_state(case.factory, case), kind=kind, deadline=False, audit_error=True)

    database(check)
