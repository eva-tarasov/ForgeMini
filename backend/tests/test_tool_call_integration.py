"""Opt-in verification on a dedicated migrated forge_task06_week4_audit_* database."""

import asyncio
import json
import os
import re
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus, ToolCallStatus
from app.domain.tool_call_transitions import InvalidToolCallTransition
from app.permissions.context import PathScope, PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError, transition_locked_agent_run
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import ToolCallService, lock_tool_call_run, transition_locked_tool_call
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import RegisteredTool, ToolRegistry
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor, create_fixture_patch_tool
from app.tools.repository.filesystem import SafeRepositoryReader
from app.tools.repository.schemas import ReadFileOutput
from app.tools.repository.writer import SafeRepositoryWriter


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TOOL_CALL_TEST_DB") is None,
    reason="set FORGE_TOOL_CALL_TEST_DB and POSTGRES_DB to opt in to Step 06 audit tests",
)

SENTINEL = "synthetic_tool_audit_secret"


class SampleInput(ToolInput):
    label: str


class SampleOutput(ToolOutput):
    count: int


def sample_executor(handler, *, timeout_ms=1000):
    spec = ToolSpec(
        name="sample_read", version=2, description="Synthetic audit integration test",
        input_schema=SampleInput, output_schema=SampleOutput, capability=Capability.REPOSITORY_READ,
        risk_level=ToolRiskLevel.LOW, timeout_ms=timeout_ms, side_effect=False, requires_approval=False,
    )
    return ToolExecutor(ToolRegistry([RegisteredTool(spec, handler, synthetic=True)]))


async def forbidden_handler(_argument):
    pytest.fail("This scenario must not invoke the handler")


@dataclass(frozen=True)
class DatabaseCase:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    context: PermissionContext
    task_id: UUID


@pytest.fixture
def database():
    name = os.getenv("FORGE_TOOL_CALL_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_week4_audit_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TOOL_CALL_TEST_DB must name the same forge_task06_week4_audit_* database", pytrace=False)
    settings = Settings(postgres_db=name)

    def run(scenario, *, status=AgentRunStatus.RUNNING):
        async def check():
            engine = create_engine(settings)
            factory = create_session_factory(engine)
            run_id, task_id, workspace_id = uuid4(), uuid4(), uuid4()
            context = PermissionContext(
                run_id, workspace_id, frozenset({Capability.REPOSITORY_READ}),
                read_scopes=(PathScope("allowed.txt"),),
            )
            seeded = False
            try:
                async with asyncio.timeout(30):
                    async with factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                        session.add(WorkspaceModel(id=workspace_id, name="synthetic audit workspace"))
                        await session.flush()
                        session.add(TaskModel(
                            id=task_id, workspace_id=workspace_id, goal="ToolCall audit verification",
                            acceptance_criteria=["metadata only"],
                        ))
                        await session.flush()
                        session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status=status.value))
                        seeded = True
                    await scenario(DatabaseCase(settings, factory, context, task_id))
            finally:
                try:
                    if seeded:
                        async with asyncio.timeout(10), factory.begin() as session:
                            assert await session.scalar(text("SELECT current_database()")) == name
                            await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == run_id))
                            await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                            await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                            await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
                finally:
                    await asyncio.wait_for(engine.dispose(), timeout=10)

        asyncio.run(check())

    return run


async def snapshot(case, *, fresh_engine=False):
    async def read(factory):
        run_id = case.context.run_id
        async with factory() as session:
            run_status = await session.scalar(select(AgentRunModel.status).where(AgentRunModel.id == run_id))
            task_status = await session.scalar(select(TaskModel.status).where(TaskModel.id == case.task_id))
            calls = (await session.execute(
                select(ToolCallModel.id, ToolCallModel.tool_name, ToolCallModel.status)
                .where(ToolCallModel.agent_run_id == run_id)
            )).all()
            events = (await session.execute(
                select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload, AgentEventModel.created_at)
                .where(AgentEventModel.agent_run_id == run_id)
                .order_by(AgentEventModel.sequence)
            )).all()
            return run_status, task_status, calls, events

    if not fresh_engine:
        return await read(case.factory)
    engine = create_engine(case.settings)
    try:
        return await read(create_session_factory(engine))
    finally:
        await engine.dispose()


def assert_audit(snapshot_value, statuses, *, error=None, duration=False, name="sample_read", version=2):
    _, task_status, calls, events = snapshot_value
    assert task_status == "queued"
    assert len(calls) == 1
    call = calls[0]
    assert (call.tool_name, call.status) == (name, statuses[-1])
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    audit = [event for event in events if event.event_type.startswith("tool_call_")]
    assert [event.payload["status"] for event in audit] == statuses
    for index, (event, status) in enumerate(zip(audit, statuses, strict=True)):
        expected = {
            "schema_version": 1, "tool_call_id": str(call.id), "tool_name": name,
            "tool_version": version, "status": status,
        }
        if index == len(statuses) - 1:
            if error is not None:
                expected["error_code"] = error
            if duration:
                elapsed = event.payload["duration_ms"]
                assert type(elapsed) is int and elapsed >= 0
                expected["duration_ms"] = elapsed
        assert event.payload == expected
        assert event.created_at is not None
        expected_type = {"proposed": "tool_call_proposed", "running": "tool_call_started"}.get(
            status, "tool_call_finished",
        )
        assert event.event_type == expected_type
        assert SENTINEL not in json.dumps(event.payload)


async def cancel_parent(case):
    async with case.factory() as session:
        await AgentRunService(session).cancel_execution(case.context.run_id)


async def cleanup_task(task):
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


def test_postgres_audited_read_file_persists_metadata_only_history(database, tmp_path):
    async def check(case):
        original = f"{SENTINEL} from authorized content\n"
        (tmp_path / "allowed.txt").write_text(original, encoding="utf-8")
        executor = create_fixture_executor(
            root=tmp_path, context=case.context, baseline=FixtureBaseline("test-revision", {}),
        )
        wrapper = AuditedToolExecutor(executor, ToolCallService(case.factory))
        output = await wrapper.execute("read_file", {"path": "allowed.txt"}, context=case.context)

        assert output == ReadFileOutput(path="allowed.txt", content=original, size_bytes=len(original.encode()))
        view = await snapshot(case, fresh_engine=True)
        assert view[0] == "running"
        assert_audit(view, ["proposed", "running", "succeeded"], duration=True, name="read_file", version=1)

    database(check)


def test_postgres_audit_denies_unauthorized_fixture_before_read(database, tmp_path, monkeypatch):
    async def check(case):
        (tmp_path / SENTINEL).write_text("synthetic unauthorized data", encoding="utf-8")
        executor = create_fixture_executor(
            root=tmp_path, context=case.context, baseline=FixtureBaseline("test-revision", {}),
        )
        wrapper = AuditedToolExecutor(executor, ToolCallService(case.factory))

        def refused(*_args, **_kwargs):
            pytest.fail("Authorization denial must not reach the fixture reader")

        monkeypatch.setattr(SafeRepositoryReader, "read_text", refused)
        with pytest.raises(ToolExecutionError, match="^permission_denied$"):
            await wrapper.execute("read_file", {"path": SENTINEL}, context=case.context)
        assert_audit(await snapshot(case), ["proposed", "denied"], error="permission_denied", name="read_file", version=1)

    database(check)


def test_postgres_audit_blocks_actual_patch_tool_without_starting_or_writing(database, tmp_path, monkeypatch):
    async def check(case):
        file = tmp_path / "allowed.txt"
        file.write_bytes(b"before\n")
        context = replace(
            case.context,
            capabilities=frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE}),
            write_scopes=(PathScope("allowed.txt"),),
        )
        tool = create_fixture_patch_tool(root=tmp_path, context=context)
        wrapper = AuditedToolExecutor(ToolExecutor(ToolRegistry([tool])), ToolCallService(case.factory))

        def no_write(*_args, **_kwargs):
            pytest.fail("An audited write proposal must not execute its handler")

        monkeypatch.setattr(SafeRepositoryWriter, "apply", no_write)
        patch = f"--- a/allowed.txt\n+++ b/allowed.txt\n@@ -1,1 +1,1 @@\n-before\n+{SENTINEL}\n"
        with pytest.raises(ToolExecutionError, match="^execution_blocked$"):
            await wrapper.execute("apply_patch", {"patch": patch}, context=context)

        view = await snapshot(case, fresh_engine=True)
        assert view[0] == "running"
        assert_audit(view, ["proposed", "denied"], error="execution_blocked", name="apply_patch", version=1)
        assert [event.event_type for event in view[3]] == ["tool_call_proposed", "tool_call_finished"]
        assert all("allowed.txt" not in json.dumps(event.payload) for event in view[3])
        assert file.read_bytes() == b"before\n" and list(tmp_path.iterdir()) == [file]

    database(check)


@pytest.mark.parametrize("code", ["version_mismatch", "invalid_input"])
def test_postgres_audit_records_prestart_failure_with_registered_version(database, code):
    async def check(case):
        wrapper = AuditedToolExecutor(sample_executor(forbidden_handler), ToolCallService(case.factory))
        arguments = {"label": SENTINEL, "untrusted-path": SENTINEL} if code == "invalid_input" else {"label": SENTINEL}
        with pytest.raises(ToolExecutionError, match=f"^{code}$"):
            await wrapper.execute(
                "sample_read", arguments, context=case.context, version=999 if code == "version_mismatch" else None,
            )
        assert_audit(await snapshot(case), ["proposed", "failed"], error=code)

    database(check)


@pytest.mark.parametrize("code", ["handler_failed", "tool_timeout", "invalid_output", "cancelled"])
def test_postgres_audit_records_started_failures_and_cancellation(database, code):
    async def check(case):
        started, release = asyncio.Event(), asyncio.Event()
        invocations = 0

        async def handler(_input):
            nonlocal invocations
            invocations += 1
            started.set()
            if code == "handler_failed":
                raise RuntimeError(SENTINEL)
            if code == "invalid_output":
                return {"count": SENTINEL}
            await release.wait()
            return SampleOutput(count=1)

        executor = sample_executor(handler, timeout_ms=25 if code == "tool_timeout" else 5000)
        wrapper = AuditedToolExecutor(executor, ToolCallService(case.factory))
        task = asyncio.create_task(wrapper.execute("sample_read", {"label": SENTINEL}, context=case.context))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            if code == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ToolExecutionError, match=f"^{code}$"):
                    await task
            assert invocations == 1
            view = await snapshot(case, fresh_engine=True)
            assert view[0] == "running"
            assert_audit(view, ["proposed", "running", "failed"], error=code, duration=True)
        finally:
            release.set()
            await cleanup_task(task)

    database(check)


@pytest.mark.parametrize("kind", ["workspace", "missing", "terminal"])
def test_postgres_audit_rejects_wrong_workspace_and_unavailable_runs(database, kind):
    async def check(case):
        context = case.context
        if kind == "workspace":
            context = replace(context, workspace_id=uuid4())
        elif kind == "missing":
            context = replace(context, run_id=uuid4())
        wrapper = AuditedToolExecutor(sample_executor(forbidden_handler), ToolCallService(case.factory))
        error = "permission_denied" if kind == "workspace" else "run_not_running"
        with pytest.raises((ToolExecutionError, ToolCallAuditError), match=f"^{error}$"):
            await wrapper.execute("sample_read", {"label": "fixture"}, context=context)
        assert (await snapshot(case))[2:] == ([], [])

    database(check, status=AgentRunStatus.CANCELLED if kind == "terminal" else AgentRunStatus.RUNNING)


def test_postgres_running_commit_is_visible_without_audit_session_during_handler(database):
    async def check(case):
        entered, release = asyncio.Event(), asyncio.Event()
        open_sessions = set()

        class TrackedSession(AsyncSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                open_sessions.add(self)

            async def close(self):
                try:
                    await super().close()
                finally:
                    open_sessions.remove(self)

        audit_engine = create_engine(case.settings)
        tracked_factory = async_sessionmaker(audit_engine, class_=TrackedSession, expire_on_commit=False)

        async def handler(_input):
            entered.set()
            await release.wait()
            return SampleOutput(count=1)

        wrapper = AuditedToolExecutor(sample_executor(handler), ToolCallService(tracked_factory))
        task = asyncio.create_task(wrapper.execute("sample_read", {"label": "fixture"}, context=case.context))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert not open_sessions and audit_engine.pool.checkedout() == 0
            assert_audit(await snapshot(case, fresh_engine=True), ["proposed", "running"])
            release.set()
            assert await task == SampleOutput(count=1)
            assert not open_sessions and audit_engine.pool.checkedout() == 0
            assert_audit(await snapshot(case, fresh_engine=True), ["proposed", "running", "succeeded"], duration=True)
        finally:
            release.set()
            await cleanup_task(task)
            await audit_engine.dispose()

    database(check)


def test_postgres_cancellation_between_proposal_and_start_prevents_admission(database):
    async def check(case):
        proposed, release = asyncio.Event(), asyncio.Event()

        class PausedService(ToolCallService):
            async def propose(self, context, spec):
                ticket = await super().propose(context, spec)
                proposed.set()
                await release.wait()
                return ticket

        wrapper = AuditedToolExecutor(sample_executor(forbidden_handler), PausedService(case.factory))
        task = asyncio.create_task(wrapper.execute("sample_read", {"label": "fixture"}, context=case.context))
        try:
            await asyncio.wait_for(proposed.wait(), timeout=3)
            await cancel_parent(case)
            release.set()
            with pytest.raises(ToolCallAuditError, match="^run_not_running$"):
                await task
            view = await snapshot(case)
            assert view[0] == "cancelled"
            assert_audit(view, ["proposed", "failed"], error="run_not_running")
        finally:
            release.set()
            await cleanup_task(task)

    database(check)


@pytest.mark.parametrize("interrupt_handler", [False, True], ids=["observed-success", "cancelled-handler"])
def test_postgres_finishes_observed_outcome_after_parent_cancellation(database, interrupt_handler):
    async def check(case):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(_input):
            entered.set()
            await release.wait()
            return SampleOutput(count=1)

        wrapper = AuditedToolExecutor(sample_executor(handler), ToolCallService(case.factory))
        task = asyncio.create_task(wrapper.execute("sample_read", {"label": "fixture"}, context=case.context))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            await cancel_parent(case)
            if interrupt_handler:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert await task == SampleOutput(count=1)
            view = await snapshot(case, fresh_engine=True)
            assert view[0] == "cancelled"
            assert_audit(
                view, ["proposed", "running", "failed" if interrupt_handler else "succeeded"],
                error="cancelled" if interrupt_handler else None, duration=True,
            )
        finally:
            release.set()
            await cleanup_task(task)

    database(check)


def test_postgres_cancelled_start_resolves_actual_committed_running_state(database):
    async def check(case):
        original = asyncio.CancelledError("interrupted admission acknowledgement")

        class InterruptedStart(ToolCallService):
            async def start(self, ticket):
                await super().start(ticket)
                raise original

        wrapper = AuditedToolExecutor(sample_executor(forbidden_handler), InterruptedStart(case.factory))
        with pytest.raises(asyncio.CancelledError) as caught:
            await wrapper.execute("sample_read", {"label": "fixture"}, context=case.context)
        assert caught.value is original
        assert_audit(await snapshot(case), ["proposed", "running", "failed"], error="cancelled")

    database(check)


def test_postgres_terminal_tool_call_cannot_be_reopened_or_overwritten(database):
    async def check(case):
        service = ToolCallService(case.factory)
        spec = sample_executor(forbidden_handler).get_tool_spec("sample_read")
        ticket = await service.propose(case.context, spec)
        await service.start(ticket)
        await service.finish(ticket, ToolCallStatus.SUCCEEDED, duration_ms=0)
        before = await snapshot(case)
        with pytest.raises(InvalidToolCallTransition):
            await service.start(ticket)
        with pytest.raises(InvalidToolCallTransition):
            await service.finish(ticket, ToolCallStatus.FAILED, error_code=ToolCallErrorCode.INTERNAL_ERROR)
        assert await snapshot(case) == before

    database(check)


def test_postgres_locked_tool_call_is_run_scoped_and_refreshes_existing_state(database):
    async def check(case):
        service = ToolCallService(case.factory)
        spec = sample_executor(forbidden_handler).get_tool_spec("sample_read")
        ticket = await service.propose(case.context, spec)
        async with case.factory() as session:
            repository = ToolCallRepository(session)
            stale = await repository.get(ticket.run_id, ticket.tool_call_id)
            assert stale.status == "proposed"
            assert await repository.get(uuid4(), ticket.tool_call_id) is None
            await service.start(ticket)
            await AgentRunRepository(session).get_for_update(ticket.run_id)
            locked = await repository.get_for_update(ticket.run_id, ticket.tool_call_id)
            assert locked is stale and locked.status == "running"

    database(check)


@pytest.mark.parametrize("resolution", ["commit", "rollback", "event-error"])
def test_postgres_transaction_local_transitions_are_atomic(database, monkeypatch, resolution):
    async def check(case):
        service = ToolCallService(case.factory)
        spec = sample_executor(forbidden_handler).get_tool_spec("sample_read")
        ticket = await service.propose(case.context, spec)
        before = await snapshot(case, fresh_engine=True)

        if resolution == "event-error":
            original_append = AgentEventRepository.append

            async def duplicate_sequence(repo, run_id, *, event_type, payload):
                event = await original_append(repo, run_id, event_type=event_type, payload=payload)
                if event_type == "tool_call_finished":
                    event.sequence = 1
                return event

            monkeypatch.setattr(AgentEventRepository, "append", duplicate_sequence)

        async with case.factory() as session:
            transaction = await session.begin()
            run = await lock_tool_call_run(session, ticket, require_running=True)
            locked_call = await ToolCallRepository(session).get_for_update(ticket.run_id, ticket.tool_call_id)
            assert locked_call is not None
            events = AgentEventRepository(session)
            await transition_locked_agent_run(events, run, AgentRunStatus.RUNNING, AgentRunStatus.FAILED)
            await transition_locked_tool_call(
                session, ticket, locked_call, ToolCallStatus.FAILED, error_code=ToolCallErrorCode.HANDLER_FAILED,
            )
            with pytest.raises(StaleAgentRunStatusError):
                await transition_locked_agent_run(events, run, AgentRunStatus.RUNNING, AgentRunStatus.CANCELLED)
            with pytest.raises(InvalidToolCallTransition):
                await transition_locked_tool_call(session, ticket, locked_call, ToolCallStatus.RUNNING)

            assert session.get_transaction() is transaction and transaction.is_active
            assert await snapshot(case, fresh_engine=True) == before
            if resolution == "event-error":
                with pytest.raises(IntegrityError):
                    await session.flush()
            if resolution == "commit":
                await session.commit()
            else:
                await session.rollback()
                await session.rollback()

        after = await snapshot(case, fresh_engine=True)
        if resolution == "commit":
            assert after[0] == "failed"
            assert_audit(after, ["proposed", "failed"], error="handler_failed")
            assert [(event.sequence, event.event_type) for event in after[3]] == [
                (1, "tool_call_proposed"), (2, "agent_run_status_changed"), (3, "tool_call_finished"),
            ]
            assert after[3][1].payload == {"from": "running", "to": "failed"}
        else:
            assert after == before

    database(check)


def test_postgres_concurrent_audit_writers_wait_on_parent_lock_and_preserve_sequence(database, monkeypatch):
    async def check(case):
        service = ToolCallService(case.factory)
        spec = sample_executor(forbidden_handler).get_tool_spec("sample_read")
        first = await service.propose(case.context, spec)
        second = await service.propose(case.context, spec)
        first_locked, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pids = {}
        append = AgentEventRepository.append
        lock = AgentRunRepository.get_for_update

        async def hold_first(repo, run_id, *, event_type, payload):
            event = await append(repo, run_id, event_type=event_type, payload=payload)
            if payload["tool_call_id"] == str(first.tool_call_id):
                pids["first"] = await repo.session.scalar(text("SELECT pg_backend_pid()"))
                first_locked.set()
                await release.wait()
            return event

        async def watch_second(repo, run_id):
            if asyncio.current_task().get_name() == "second-tool-start":
                pids["second"] = await repo.session.scalar(text("SELECT pg_backend_pid()"))
                second_started.set()
            return await lock(repo, run_id)

        monkeypatch.setattr(AgentEventRepository, "append", hold_first)
        monkeypatch.setattr(AgentRunRepository, "get_for_update", watch_second)
        first_task = asyncio.create_task(service.start(first))
        second_task = None
        try:
            await asyncio.wait_for(first_locked.wait(), timeout=5)
            second_task = asyncio.create_task(service.start(second), name="second-tool-start")
            async with asyncio.timeout(5):
                await second_started.wait()
                async with case.factory() as observer:
                    while True:
                        blockers = await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["second"]},
                        )
                        if pids["first"] in blockers:
                            break
                        assert not second_task.done()
                        await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(first_task, second_task)
        finally:
            release.set()
            await cleanup_task(first_task)
            if second_task is not None:
                await cleanup_task(second_task)

        _, _, calls, events = await snapshot(case, fresh_engine=True)
        assert {call.id: call.status for call in calls} == {first.tool_call_id: "running", second.tool_call_id: "running"}
        assert [(event.sequence, event.payload["tool_call_id"], event.payload["status"]) for event in events] == [
            (1, str(first.tool_call_id), "proposed"), (2, str(second.tool_call_id), "proposed"),
            (3, str(first.tool_call_id), "running"), (4, str(second.tool_call_id), "running"),
        ]

    database(check)


@pytest.mark.parametrize("phase", ["proposed", "started", "finished"])
@pytest.mark.parametrize("failure_at", ["append", "commit"])
def test_postgres_real_event_constraint_failure_rolls_back_lifecycle_mutation(
    database, monkeypatch, phase, failure_at,
):
    async def check(case):
        invocations = 0
        injected = []
        original_append = AgentEventRepository.append

        async def invalid_event(repo, run_id, *, event_type, payload):
            event = await original_append(repo, run_id, event_type=event_type, payload=payload)
            if event_type == f"tool_call_{phase}":
                injected.append(event_type)
                # The existing PostgreSQL sequence >= 1 CHECK fails when this event is flushed.
                event.sequence = 0
                if failure_at == "append":
                    await repo.session.flush()
            return event

        async def handler(_input):
            nonlocal invocations
            invocations += 1
            return SampleOutput(count=1)

        monkeypatch.setattr(AgentEventRepository, "append", invalid_event)
        wrapper = AuditedToolExecutor(sample_executor(handler), ToolCallService(case.factory))
        with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$") as caught:
            await wrapper.execute("sample_read", {"label": SENTINEL}, context=case.context)
        assert caught.value.__suppress_context__
        assert injected == [f"tool_call_{phase}"]
        view = await snapshot(case, fresh_engine=True)
        assert view[0] == "running"
        if phase == "proposed":
            assert invocations == 0 and view[2:] == ([], [])
        elif phase == "started":
            assert invocations == 0
            assert_audit(view, ["proposed", "failed"], error="audit_persistence_failed")
        else:
            assert invocations == 1
            assert_audit(view, ["proposed", "running"])

    database(check)
