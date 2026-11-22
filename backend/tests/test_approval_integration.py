"""PostgreSQL verification of the one-shot fixture approval path."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope, PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.approvals import ApprovalRepository
from app.services.agent_runs import AgentRunService, transition_locked_agent_run
from app.services.approval_contracts import ApprovalDecision, PendingOperation, patch_fingerprint, validate_patch_proposal
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import ToolCallService
from app.tools.errors import ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository import handlers
from app.tools.repository.factory import create_fixture_patch_tool
from app.tools.repository.schemas import ApplyPatchOutput
from app.tools.repository.writer import SafeRepositoryWriter
from test_approval_contracts import patch_for


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_APPROVAL_TEST_DB") is None,
    reason="set FORGE_APPROVAL_TEST_DB and POSTGRES_DB to opt in to Step 08 approval tests",
)

SENTINEL = "synthetic_approval_secret"
TARGET = "fixture.txt"
ORIGINAL = b"before\noutside hunk\n"


@dataclass
class Fixture:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    context: PermissionContext
    task_id: UUID
    root: Path
    executor: ToolExecutor
    coordinator: ApprovedToolExecutor

    @property
    def target(self):
        return self.root / TARGET


@pytest.fixture
def database(tmp_path):
    name = os.getenv("FORGE_APPROVAL_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_week4_audit_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB must match FORGE_APPROVAL_TEST_DB and the dedicated forge_task06_week4_audit_* guard")
    settings = Settings(postgres_db=name)

    def run(scenario, *, status=AgentRunStatus.RUNNING, wait_seconds=10):
        async def check():
            engine = create_engine(settings)
            factory = create_session_factory(engine)
            workspace_id, task_id, run_id = uuid4(), uuid4(), uuid4()
            scopes = (PathScope(".", recursive=True),)
            context = PermissionContext(
                run_id, workspace_id, frozenset(Capability), read_scopes=scopes, write_scopes=scopes,
            )
            (tmp_path / TARGET).write_bytes(ORIGINAL)
            (tmp_path / "unrelated.txt").write_bytes(b"unrelated\n")
            tool = create_fixture_patch_tool(root=tmp_path, context=context)
            executor = ToolExecutor(ToolRegistry([tool]))
            seeded = False
            try:
                async with asyncio.timeout(20):
                    async with factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "a7c3e91d4b20"
                        session.add(WorkspaceModel(id=workspace_id, name="synthetic approval fixture"))
                        await session.flush()
                        session.add(TaskModel(
                            id=task_id, workspace_id=workspace_id, goal="verify fixture approval",
                            acceptance_criteria=["exact operation"],
                        ))
                        await session.flush()
                        session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status=status.value))
                        seeded = True
                    coordinator = ApprovedToolExecutor(executor, factory, wait_seconds=wait_seconds)
                    await scenario(Fixture(settings, factory, context, task_id, tmp_path, executor, coordinator))
            finally:
                try:
                    if seeded:
                        async with asyncio.timeout(5), factory.begin() as session:
                            assert await session.scalar(text("SELECT current_database()")) == name
                            call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == run_id)
                            await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
                            await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == run_id))
                            await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                            await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                            await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
                finally:
                    await engine.dispose()

        asyncio.run(check())

    return run


async def snapshot(case, *, fresh=False):
    async def read(factory):
        run_id = case.context.run_id
        async with factory() as session:
            run = await session.scalar(select(AgentRunModel.status).where(AgentRunModel.id == run_id))
            task = await session.scalar(select(TaskModel.status).where(TaskModel.id == case.task_id))
            calls = (await session.execute(
                select(ToolCallModel.id, ToolCallModel.status).where(ToolCallModel.agent_run_id == run_id)
            )).all()
            approvals = (await session.execute(
                select(ApprovalModel.id, ApprovalModel.status, ApprovalModel.decided_at)
                .join(ToolCallModel, ToolCallModel.id == ApprovalModel.tool_call_id)
                .where(ToolCallModel.agent_run_id == run_id)
            )).all()
            events = (await session.execute(
                select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload)
                .where(AgentEventModel.agent_run_id == run_id)
                .order_by(AgentEventModel.sequence)
            )).all()
            return run, task, calls, approvals, events

    if not fresh:
        return await read(case.factory)
    engine = create_engine(case.settings)
    try:
        return await read(create_session_factory(engine))
    finally:
        await engine.dispose()


def assert_lifecycle(view, op, *, run, call, approval, terminal_code=None):
    run_status, task_status, calls, approvals, events = view
    assert (run_status, task_status) == (run, "queued")
    assert calls == [(op.reference.ticket.tool_call_id, call)]
    assert len(approvals) == 1
    assert (approvals[0].id, approvals[0].status) == (op.reference.approval_id, approval)
    assert (approvals[0].decided_at is None) == (approval == "pending")
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.payload for event in events if event.event_type == "approval_requested"] == [
        op.reference.requested_payload(),
    ]
    assert SENTINEL not in json.dumps([event.payload for event in events])
    assert TARGET not in json.dumps([event.payload for event in events])
    audit = [event.payload for event in events if event.event_type.startswith("tool_call_")]
    assert all(event["tool_call_id"] == str(op.reference.ticket.tool_call_id) for event in audit)
    assert audit[-1]["status"] == call and audit[-1].get("error_code") == terminal_code
    return [event.event_type for event in events]


@asynccontextmanager
async def waiting(case, monkeypatch, *, patch=None):
    coordinator = case.coordinator
    published = asyncio.Event()
    operations = []
    publish = coordinator._pending.publish

    def observed(operation):
        publish(operation)
        operations.append(operation)
        published.set()

    with monkeypatch.context() as changes:
        changes.setattr(coordinator._pending, "publish", observed)
        task = asyncio.create_task(coordinator.execute(
            "apply_patch", {"patch": patch if patch is not None else patch_for(TARGET, after=SENTINEL)},
            context=case.context,
        ))
        try:
            await asyncio.wait_for(published.wait(), timeout=3)
            (operation,) = operations
            assert coordinator.pending_request(case.context) is operation.reference
            yield task, operation
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


async def approve(case, operation):
    await case.coordinator.decide(operation.reference, ApprovalDecision.APPROVE, context=case.context)


def test_postgres_approval_commits_before_wait_and_notification_then_writes_once(database, monkeypatch):
    async def check(case):
        engine = create_engine(case.settings)
        sessions = set()
        opened, committed, row_locks = [], [], []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def observe_locks(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.endswith("FOR UPDATE"):
                row_locks.append(statement.split("FROM ", 1)[1].split()[0])

        class TrackedSession(AsyncSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                sessions.add(self)
                opened.append(self)

            async def commit(self):
                await super().commit()
                committed.append((self, tuple(row_locks)))
                row_locks.clear()

            async def close(self):
                try:
                    await super().close()
                finally:
                    sessions.remove(self)

        case.coordinator = ApprovedToolExecutor(
            case.executor, async_sessionmaker(engine, class_=TrackedSession, expire_on_commit=False),
        )
        writes, notifications = [], []
        write = SafeRepositoryWriter.apply
        notify = case.coordinator._pending.notify

        def observe_write(writer, patch, **kwargs):
            assert not sessions and engine.pool.checkedout() == 0
            assert len(opened) == len(committed) == 3
            writes.append(patch)
            return write(writer, patch, **kwargs)

        def pause_notification(operation, decision):
            assert not sessions and engine.pool.checkedout() == 0
            assert len(opened) == len(committed) == 2
            notifications.append((operation, decision))

        monkeypatch.setattr(SafeRepositoryWriter, "apply", observe_write)
        monkeypatch.setattr(case.coordinator._pending, "notify", pause_notification)
        try:
            async with waiting(case, monkeypatch) as (execution, operation):
                assert not sessions and engine.pool.checkedout() == 0
                assert len(opened) == len(committed) == 1
                assert assert_lifecycle(await snapshot(case, fresh=True), operation,
                                        run="waiting_approval", call="proposed", approval="pending") == [
                    "tool_call_proposed", "approval_requested", "agent_run_status_changed",
                ]
                async with case.factory() as observer:
                    approvals = ApprovalRepository(observer)
                    reference = operation.reference
                    row = await approvals.get(reference.approval_id, reference.ticket.tool_call_id)
                    assert row.status == "pending"
                    assert await approvals.get(reference.approval_id, uuid4()) is None
                assert case.target.read_bytes() == ORIGINAL
                await approve(case, operation)
                assert notifications == [(operation, ApprovalDecision.APPROVE)]
                assert not execution.done() and writes == []
                assert_lifecycle(await snapshot(case, fresh=True), operation,
                                 run="waiting_approval", call="proposed", approval="approved")
                notify(*notifications[0])
                assert await execution == ApplyPatchOutput(changed_paths=(TARGET,), changed_files=1)
                assert writes == [operation.patch]
                assert case.target.read_bytes() == f"{SENTINEL}\noutside hunk\n".encode()
                assert (case.root / "unrelated.txt").read_bytes() == b"unrelated\n"
                assert assert_lifecycle(await snapshot(case, fresh=True), operation,
                                        run="running", call="succeeded", approval="approved") == [
                    "tool_call_proposed", "approval_requested", "agent_run_status_changed", "approval_decided",
                    "agent_run_status_changed", "tool_call_started", "tool_call_finished",
                ]
            assert case.coordinator.pending_request(case.context) is None
            assert len(opened) == len(committed) == 4
            assert [session for session, _ in committed] == opened
            assert set(committed[0][1]) == {"agent_runs"}
            assert [locks[:3] for _, locks in committed[1:]] == [("agent_runs", "tool_calls", "approvals")] * 3
        finally:
            await engine.dispose()

    database(check)


@pytest.mark.parametrize("resolution", ["reject", "timeout", "cancel", "cancel-after-approve"])
def test_postgres_approval_negative_resolutions_never_write(database, monkeypatch, resolution):
    async def check(case):
        if resolution == "cancel-after-approve":
            monkeypatch.setattr(case.coordinator._pending, "notify", lambda *_args: None)
        async with waiting(case, monkeypatch) as (execution, operation):
            if resolution == "reject":
                await case.coordinator.decide(operation.reference, ApprovalDecision.REJECT, context=case.context)
                with pytest.raises(ToolExecutionError, match="^approval_rejected$"):
                    await execution
                expected = ("failed", "denied", "rejected", "approval_rejected")
            elif resolution == "timeout":
                with pytest.raises(ToolExecutionError, match="^approval_timeout$"):
                    await execution
                expected = ("failed", "failed", "rejected", "approval_timeout")
            else:
                if resolution == "cancel-after-approve":
                    await approve(case, operation)
                execution.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await execution
                expected = (
                    "cancelled", "failed", "approved" if resolution == "cancel-after-approve" else "rejected",
                    "cancelled",
                )
            assert "tool_call_started" not in assert_lifecycle(
                await snapshot(case, fresh=True), operation,
                run=expected[0], call=expected[1], approval=expected[2], terminal_code=expected[3],
            )
            assert case.target.read_bytes() == ORIGINAL
            assert case.coordinator.pending_request(case.context) is None
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await approve(case, operation)

    database(check, wait_seconds=0.5 if resolution == "timeout" else 10)


def test_postgres_approval_timeout_preserves_committed_approval_without_notification(database, monkeypatch):
    async def check(case):
        monkeypatch.setattr(case.coordinator._pending, "notify", lambda *_args: None)
        async with waiting(case, monkeypatch) as (execution, operation):
            await approve(case, operation)
            with pytest.raises(ToolExecutionError, match="^approval_timeout$"):
                await execution
            assert_lifecycle(await snapshot(case), operation, run="failed", call="failed",
                             approval="approved", terminal_code="approval_timeout")
            assert case.target.read_bytes() == ORIGINAL

    database(check, wait_seconds=0.5)


@pytest.mark.parametrize("wrong", ["approval_id", "tool_call_id", "run_id", "workspace_id", "version", "patch", "snapshot", "context"])
def test_postgres_approval_rejects_mismatched_binding_and_duplicate_decision(database, monkeypatch, wrong):
    async def check(case):
        async with waiting(case, monkeypatch) as (execution, operation):
            ref, context = operation.reference, case.context
            if wrong in ("tool_call_id", "run_id", "workspace_id", "version"):
                field = "tool_version" if wrong == "version" else wrong
                value = 2 if wrong == "version" else uuid4()
                ref = replace(ref, ticket=replace(ref.ticket, **{field: value}))
            elif wrong == "approval_id":
                ref = replace(ref, approval_id=uuid4())
            elif wrong == "patch":
                ref = replace(ref, patch_sha256=patch_fingerprint(patch_for(TARGET, after="different")))
            elif wrong == "snapshot":
                ref = replace(ref, sources_sha256="0" * 64)
            else:
                context = replace(context)
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await case.coordinator.decide(ref, ApprovalDecision.APPROVE, context=context)
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await case.coordinator.execute("apply_patch", {"patch": patch_for(TARGET, after="different")},
                                               context=case.context)
            assert_lifecycle(await snapshot(case), operation, run="waiting_approval", call="proposed", approval="pending")
            await case.coordinator.decide(operation.reference, ApprovalDecision.REJECT, context=case.context)
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await case.coordinator.decide(operation.reference, ApprovalDecision.REJECT, context=case.context)
            with pytest.raises(ToolExecutionError, match="^approval_rejected$"):
                await execution
            assert case.target.read_bytes() == ORIGINAL

    database(check)


def test_postgres_approval_claim_is_single_use_and_normal_executors_still_block(database, monkeypatch):
    async def check(case):
        invoked, release = asyncio.Event(), asyncio.Event()
        invocations = []
        invoke = case.executor._invoke_handler

        async def pause(tool, arguments, **kwargs):
            invocations.append(tool.spec.name)
            invoked.set()
            await release.wait()
            return await invoke(tool, arguments, **kwargs)

        monkeypatch.setattr(case.executor, "_invoke_handler", pause)
        async with waiting(case, monkeypatch) as (execution, operation):
            await approve(case, operation)
            await asyncio.wait_for(invoked.wait(), timeout=3)
            try:
                assert_lifecycle(await snapshot(case), operation, run="running", call="running", approval="approved")
                with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                    await case.coordinator._gate.claim(operation)
                with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                    await approve(case, operation)
                with pytest.raises(ToolExecutionError, match="^execution_blocked$"):
                    await case.executor.execute("apply_patch", {"patch": operation.patch}, context=case.context)
                with pytest.raises(ToolExecutionError, match="^invalid_input$"):
                    await case.executor.execute(
                        "apply_patch", {"patch": operation.patch, "approved": True}, context=case.context,
                    )
                audit = AuditedToolExecutor(case.executor, ToolCallService(case.factory))
                with pytest.raises(ToolExecutionError, match="^execution_blocked$"):
                    await audit.execute("apply_patch", {"patch": operation.patch}, context=case.context)
                assert invocations == ["apply_patch"] and case.target.read_bytes() == ORIGINAL
            finally:
                release.set()
            assert await execution == ApplyPatchOutput(changed_paths=(TARGET,), changed_files=1)
            _, _, calls, approvals, events = await snapshot(case)
            assert {call.status for call in calls} == {"succeeded", "denied"} and len(calls) == 2
            assert len(approvals) == 1
            assert [event.event_type for event in events].count("tool_call_started") == 1

    database(check)


def test_postgres_restart_without_live_operation_never_claims_approved_row(database, monkeypatch):
    async def check(case):
        monkeypatch.setattr(case.coordinator._pending, "notify", lambda *_args: None)
        async with waiting(case, monkeypatch) as (execution, operation):
            await approve(case, operation)
            restarted = ApprovedToolExecutor(case.executor, case.factory)
            assert restarted.pending_request(case.context) is None
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await restarted._gate.claim(operation)
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await restarted.decide(operation.reference, ApprovalDecision.APPROVE, context=case.context)
            with pytest.raises(ToolCallAuditError, match="^run_not_running$"):
                await restarted.execute("apply_patch", {"patch": operation.patch}, context=case.context)
            assert_lifecycle(await snapshot(case), operation, run="waiting_approval", call="proposed", approval="approved")
            assert case.target.read_bytes() == ORIGINAL
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution

    database(check)


@pytest.mark.parametrize("when", ["before-claim", "after-claim"])
def test_postgres_approval_detects_source_mutation_outside_hunk(database, monkeypatch, when):
    async def check(case):
        changed = b"before\nexternally modified outside hunk\n"
        if when == "after-claim":
            claim = case.coordinator._gate.claim

            async def mutate_after_claim(operation):
                await claim(operation)
                case.target.write_bytes(changed)

            monkeypatch.setattr(case.coordinator._gate, "claim", mutate_after_claim)
        async with waiting(case, monkeypatch) as (execution, operation):
            if when == "before-claim":
                case.target.write_bytes(changed)
            await approve(case, operation)
            error = "approval_source_changed" if when == "before-claim" else "handler_failed"
            with pytest.raises(ToolExecutionError, match=f"^{error}$"):
                await execution
            events = assert_lifecycle(await snapshot(case), operation, run="failed", call="failed",
                                      approval="approved", terminal_code=error)
            assert ("tool_call_started" in events) == (when == "after-claim")
            assert case.target.read_bytes() == changed

    database(check)


def test_postgres_approval_claim_checks_exact_persisted_request_metadata(database, monkeypatch):
    async def check(case):
        async with waiting(case, monkeypatch) as (execution, operation):
            notify = case.coordinator._pending.notify
            with monkeypatch.context() as changes:
                changes.setattr(case.coordinator._pending, "notify", lambda *_args: None)
                await approve(case, operation)
            async with case.factory.begin() as session:
                event = await session.scalar(select(AgentEventModel).where(
                    AgentEventModel.agent_run_id == case.context.run_id,
                    AgentEventModel.event_type == "approval_requested",
                ))
                event.payload = {**event.payload, "patch_sha256": "0" * 64}
            notify(operation, ApprovalDecision.APPROVE)
            with pytest.raises(ToolExecutionError) as caught:
                await execution
            assert str(caught.value) == "approval_invalid"
            assert caught.value.__notes__ == ["tool_call_audit_finalization_failed"]
            assert case.target.read_bytes() == ORIGINAL
            view = await snapshot(case)
            assert view[0] == "waiting_approval" and view[2][0].status == "proposed"
            assert all(event.event_type != "tool_call_started" for event in view[4])

    database(check)


def test_postgres_cancellation_after_claim_commit_never_invokes_handler(database, monkeypatch):
    async def check(case):
        claimed, release = asyncio.Event(), asyncio.Event()
        claim = case.coordinator._gate.claim

        async def suspend_after_commit(operation):
            await claim(operation)
            claimed.set()
            await release.wait()

        monkeypatch.setattr(case.coordinator._gate, "claim", suspend_after_commit)
        async with waiting(case, monkeypatch) as (execution, operation):
            await approve(case, operation)
            await asyncio.wait_for(claimed.wait(), timeout=3)
            assert_lifecycle(await snapshot(case, fresh=True), operation, run="running", call="running",
                             approval="approved")
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
            assert_lifecycle(await snapshot(case, fresh=True), operation, run="cancelled", call="failed",
                             approval="approved", terminal_code="cancelled")
            assert case.target.read_bytes() == ORIGINAL
            assert case.coordinator.pending_request(case.context) is None

    database(check)


def test_postgres_approval_decision_waits_for_parent_cancellation_lock(database, monkeypatch):
    async def check(case):
        waiter_ready = asyncio.Event()
        pids = {}
        original_lock = AgentRunRepository.get_for_update

        async def report_waiter(repository, run_id):
            if asyncio.current_task().get_name() == "approval-decision":
                pids["waiter"] = await repository.session.scalar(text("SELECT pg_backend_pid()"))
                waiter_ready.set()
            return await original_lock(repository, run_id)

        monkeypatch.setattr(AgentRunRepository, "get_for_update", report_waiter)
        async with waiting(case, monkeypatch) as (execution, operation):
            decision = None
            try:
                async with case.factory() as canceller:
                    run = await AgentRunRepository(canceller).get_for_update(case.context.run_id)
                    await transition_locked_agent_run(
                        AgentEventRepository(canceller), run, AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.CANCELLED,
                    )
                    pids["owner"] = await canceller.scalar(text("SELECT pg_backend_pid()"))
                    decision = asyncio.create_task(approve(case, operation), name="approval-decision")
                    async with asyncio.timeout(5), case.factory() as observer:
                        await waiter_ready.wait()
                        while True:
                            blockers = await observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["waiter"]},
                            )
                            if pids["owner"] in blockers:
                                break
                            assert not decision.done()
                            await asyncio.sleep(0.01)
                    await canceller.commit()
                with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                    await decision
            finally:
                if decision is not None:
                    if not decision.done():
                        decision.cancel()
                    await asyncio.gather(decision, return_exceptions=True)
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
            assert_lifecycle(await snapshot(case, fresh=True), operation, run="cancelled", call="failed",
                             approval="rejected", terminal_code="cancelled")
            assert case.target.read_bytes() == ORIGINAL

    database(check)


@pytest.mark.parametrize("started", [False, True], ids=["proposed", "running"])
def test_postgres_approval_finish_records_duration_only_after_start_and_ignores_repeat(database, started):
    async def check(case):
        tool, typed, writer = validate_patch_proposal(
            case.executor, "apply_patch", {"patch": patch_for(TARGET)}, version=None, context=case.context,
        )
        operation = PendingOperation.create(
            case.context, tool, writer, typed, writer.preview(typed.patch), asyncio.get_running_loop().time() + 10,
        )
        gate = case.coordinator._gate
        pending = case.coordinator._pending
        pending.reserve(operation)
        try:
            await gate.request(operation)
            if started:
                await approve(case, operation)
                await gate.claim(operation)
            await gate.finish(operation, error=ToolCallErrorCode.CANCELLED, duration_ms=7)
            first = await snapshot(case, fresh=True)
            assert_lifecycle(
                first, operation, run="cancelled", call="failed",
                approval="approved" if started else "rejected", terminal_code="cancelled",
            )
            (finished,) = [event.payload for event in first[4] if event.event_type == "tool_call_finished"]
            if started:
                assert finished["duration_ms"] == 7
            else:
                assert "duration_ms" not in finished
            await gate.finish(operation, error=ToolCallErrorCode.HANDLER_FAILED, duration_ms=8)
            assert await snapshot(case, fresh=True) == first
            assert case.target.read_bytes() == ORIGINAL
        finally:
            pending.discard(operation)

    database(check)


@pytest.mark.parametrize("phase", ["request", "decision", "claim", "finish"])
def test_postgres_real_event_constraint_failure_rolls_back_approval_phase(database, monkeypatch, phase):
    async def check(case):
        event_type = {
            "request": "approval_requested", "decision": "approval_decided",
            "claim": "tool_call_started", "finish": "tool_call_finished",
        }[phase]
        injected, writes = [], []
        append = AgentEventRepository.append
        write = SafeRepositoryWriter.apply

        async def fail_once(repository, run_id, *, event_type: str, payload: dict):
            row = await append(repository, run_id, event_type=event_type, payload=payload)
            if not injected and event_type == expected_type:
                injected.append(event_type)
                row.sequence = 0  # Real non-deferrable PostgreSQL CHECK at commit's flush.
            return row

        def observed_write(writer, patch, **kwargs):
            writes.append(patch)
            return write(writer, patch, **kwargs)

        expected_type = event_type
        monkeypatch.setattr(AgentEventRepository, "append", fail_once)
        monkeypatch.setattr(SafeRepositoryWriter, "apply", observed_write)
        if phase == "request":
            with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$"):
                await case.coordinator.execute("apply_patch", {"patch": patch_for(TARGET)}, context=case.context)
            assert await snapshot(case, fresh=True) == ("running", "queued", [], [], [])
        else:
            async with waiting(case, monkeypatch) as (execution, operation):
                if phase == "decision":
                    with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$"):
                        await approve(case, operation)
                    assert not execution.done()
                    assert assert_lifecycle(await snapshot(case, fresh=True), operation,
                                            run="waiting_approval", call="proposed", approval="pending") == [
                        "tool_call_proposed", "approval_requested", "agent_run_status_changed",
                    ]
                    execution.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await execution
                else:
                    await approve(case, operation)
                    with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$"):
                        await execution
                    view = await snapshot(case, fresh=True)
                    if phase == "claim":
                        event_types = assert_lifecycle(view, operation, run="failed", call="failed",
                                                       approval="approved", terminal_code="audit_persistence_failed")
                        assert "tool_call_started" not in event_types
                        assert not any(
                            event.payload.get("to") == "running" for event in view[4]
                            if event.event_type == "agent_run_status_changed"
                        )
                    else:
                        assert "tool_call_finished" not in assert_lifecycle(
                            view, operation, run="running", call="running", approval="approved",
                        )
                        assert writes == [operation.patch]
        assert injected == [expected_type]
        assert case.coordinator.pending_request(case.context) is None
        if phase == "finish":
            assert case.target.read_bytes() == f"{SENTINEL}\noutside hunk\n".encode()
        else:
            assert writes == [] and case.target.read_bytes() == ORIGINAL

    database(check)


@pytest.mark.parametrize("result", ["handler_failed", "invalid_output"])
def test_postgres_approved_handler_failure_and_invalid_output_fail_run(database, monkeypatch, result):
    async def check(case):
        if result == "handler_failed":
            def fail(*_args, **_kwargs):
                raise RuntimeError(SENTINEL)

            monkeypatch.setattr(SafeRepositoryWriter, "apply", fail)
        else:
            monkeypatch.setattr(handlers, "ApplyPatchOutput", lambda **_kwargs: {"invalid": SENTINEL})
        async with waiting(case, monkeypatch) as (execution, operation):
            await approve(case, operation)
            with pytest.raises(ToolExecutionError, match=f"^{result}$"):
                await execution
            assert_lifecycle(await snapshot(case, fresh=True), operation, run="failed", call="failed",
                             approval="approved", terminal_code=result)
            expected = ORIGINAL if result == "handler_failed" else f"{SENTINEL}\noutside hunk\n".encode()
            assert case.target.read_bytes() == expected

    database(check)


def test_postgres_approval_reports_partial_apply_without_retry_or_reuse(database, monkeypatch):
    async def check(case):
        second = case.root / "second.txt"
        second.write_bytes(ORIGINAL)
        calls = []
        rename = os.replace

        def fail_second(source, target, **kwargs):
            calls.append(target)
            if target == "second.txt":
                raise OSError(SENTINEL)
            return rename(source, target, **kwargs)

        monkeypatch.setattr(os, "replace", fail_second)
        async with waiting(case, monkeypatch, patch=patch_for(TARGET, "second.txt", after=SENTINEL)) as (execution, op):
            await approve(case, op)
            with pytest.raises(ToolExecutionError, match="^patch_partial_apply$") as caught:
                await execution
            assert caught.value.__suppress_context__
            assert calls == [TARGET, "second.txt"]
            assert case.target.read_bytes() == f"{SENTINEL}\noutside hunk\n".encode()
            assert second.read_bytes() == ORIGINAL
            assert not list(case.root.glob(".forge-patch-*"))
            assert_lifecycle(await snapshot(case, fresh=True), op, run="failed", call="failed",
                             approval="approved", terminal_code="patch_partial_apply")
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await approve(case, op)

    database(check)


@pytest.mark.parametrize("missing", ["read-capability", "write-capability", "read-scope", "write-scope"])
def test_postgres_missing_authority_cannot_create_approval(database, missing):
    async def check(case):
        context = case.context
        if missing.endswith("capability"):
            capability = Capability.REPOSITORY_WRITE if missing.startswith("read") else Capability.REPOSITORY_READ
            context = replace(context, capabilities=frozenset({capability}))
        elif missing == "read-scope":
            context = replace(context, read_scopes=())
        else:
            context = replace(context, write_scopes=())
        executor = ToolExecutor(ToolRegistry([create_fixture_patch_tool(root=case.root, context=context)]))
        coordinator = ApprovedToolExecutor(executor, case.factory)
        with pytest.raises(ToolExecutionError, match="^permission_denied$"):
            await coordinator.execute("apply_patch", {"patch": patch_for(TARGET)}, context=context)
        assert await snapshot(case) == ("running", "queued", [], [], [])
        assert case.target.read_bytes() == ORIGINAL

    database(check)


def test_postgres_terminal_parent_cannot_open_or_reopen_approval(database):
    async def check(case):
        with pytest.raises(ToolCallAuditError, match="^run_not_running$"):
            await case.coordinator.execute("apply_patch", {"patch": patch_for(TARGET)}, context=case.context)
        assert await snapshot(case, fresh=True) == ("cancelled", "queued", [], [], [])
        assert case.target.read_bytes() == ORIGINAL

    database(check, status=AgentRunStatus.CANCELLED)
