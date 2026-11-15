"""Opt-in lifecycle tests against a dedicated, already migrated PostgreSQL database."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langsmith import tracing_context
from model_test_support import TRACING_OFF
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.agent_runtime.agent_graph import create_agent_graph
from app.agent_runtime.fixture_tools import read_fixture
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.structured_output import TaskAnalysis
from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus as Status
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.agent_failure import AgentFailure, AgentFailureCode, AgentFailureContractError, RetryDisposition
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_result_codec import encode_agent_result
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import (
    AgentRunNotFoundError,
    AgentRunResultAlreadyRecordedError,
    AgentRunService,
    StaleAgentRunStatusError,
)
from app.services.agent_service import AgentService


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)

GOAL = "Проверить область действия ключа"
CRITERIA = ("Сохранить порядок", "Отделить fixture от допущений")
FIXTURE_TEXT = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)


@pytest.fixture(autouse=True)
def disable_model_tracing(monkeypatch):
    for key, value in TRACING_OFF.items():
        monkeypatch.setenv(key, value)
    with tracing_context(enabled=False, parent=False):
        yield


@pytest.fixture
def database():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    settings = Settings(postgres_db=name)

    @asynccontextmanager
    async def seeded(initial):
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        workspace_id, task_id, run_id = uuid4(), uuid4(), uuid4()
        request = AgentRunRequest(task_id, run_id, GOAL, CRITERIA)
        seeded = False
        try:
            async with asyncio.timeout(30):
                async with factory() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic agent service"))
                    await session.flush()
                    session.add(TaskModel(
                        id=task_id, workspace_id=workspace_id, goal=GOAL,
                        acceptance_criteria=list(CRITERIA), status="queued",
                    ))
                    await session.flush()
                    session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status=initial.value))
                    await session.flush()
                    seeded = True  # Clean these IDs even if the commit outcome is uncertain.
                    await session.commit()
                yield factory, request, workspace_id
        finally:
            try:
                if seeded:
                    async with asyncio.timeout(10), factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                        await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                        await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
            finally:
                await asyncio.wait_for(engine.dispose(), timeout=10)

    def run(scenario, *, initial=Status.QUEUED):
        async def check():
            async with seeded(initial) as data:
                await scenario(*data)

        asyncio.run(check())

    return run


def _result(*, blocker=None, observations=()):
    return AgentFinalResult(
        summary="Предварительный анализ",
        status="ready_for_verification" if blocker is None else "blocked",
        relevant_components=(),
        assumptions=(),
        observations=observations,
        blocked_reason=blocker,
    )


async def _assert_persisted(factory, request, workspace_id, status, *edges, result=None, failure=None):
    async with factory() as session:
        task = await session.get(TaskModel, request.task_id)
        run = await session.get(AgentRunModel, request.run_id)
        events = (await session.scalars(
            select(AgentEventModel)
            .where(AgentEventModel.agent_run_id == request.run_id)
            .order_by(AgentEventModel.sequence)
        )).all()
        assert (run.status, run.task_id, run.attempt_number) == (status.value, request.task_id, 1)
        assert (task.workspace_id, task.goal, tuple(task.acceptance_criteria), task.status, task.idempotency_key) == (
            workspace_id, GOAL, CRITERIA, "queued", None,
        )
        expected = [
            ("agent_run_status_changed", {"from": source.value, "to": target.value})
            for source, target in edges
        ]
        if result is None:
            assert await session.scalar(
                select(AgentRunModel.final_result.is_(None)).where(AgentRunModel.id == run.id)
            ) is True
        else:
            assert run.final_result == encode_agent_result(result)
            expected.insert(-1, ("agent_result_recorded", {"schema_version": 1, "result_status": result.status}))
        if failure is not None:
            expected.insert(-1, ("agent_execution_failed", {"schema_version": 1, "code": failure.value}))
        assert [
            (event.sequence, event.event_type, event.payload) for event in events
        ] == [
            (number, event_type, payload) for number, (event_type, payload) in enumerate(expected, 1)
        ]


CLAIM = (Status.QUEUED, Status.RUNNING)


@pytest.mark.parametrize("blocker,target", [(None, Status.VERIFYING), ("Нужны данные", Status.FAILED)])
def test_agent_service_commits_claim_before_runner_and_finishes_in_new_session(database, blocker, target):
    async def check(factory, request, workspace_id):
        sessions = []
        closed = []
        expected_result = _result(blocker=blocker)

        @asynccontextmanager
        async def tracked_factory():
            async with factory() as session:
                assert not session.identity_map
                sessions.append(session)
                yield session
            assert not session.in_transaction()
            closed.append(session)

        async def run(snapshot):
            assert snapshot == request
            assert len(sessions) == 1 and closed == sessions
            await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
            async with factory() as observer:
                # A live claim lock would make PostgreSQL reject this NOWAIT query.
                lock = select(AgentRunModel.id).where(AgentRunModel.id == request.run_id).with_for_update(nowait=True)
                assert await observer.scalar(lock) == request.run_id
            return expected_result

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        result = await AgentService(tracked_factory, runner).execute_run(request.run_id)

        assert result is expected_result
        runner.run.assert_awaited_once_with(request)
        assert len(sessions) == 2 and sessions[0] is not sessions[1]
        assert closed == sessions
        await _assert_persisted(factory, request, workspace_id, target, CLAIM, (Status.RUNNING, target), result=result)

    database(check)


def test_agent_service_rejects_missing_run(database):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        with pytest.raises(AgentRunNotFoundError):
            await AgentService(factory, runner).execute_run(uuid4())
        runner.run.assert_not_awaited()
        await _assert_persisted(factory, request, workspace_id, Status.QUEUED)

    database(check)


@pytest.mark.parametrize("initial", [status for status in Status if status is not Status.QUEUED])
def test_agent_service_rejects_nonqueued_run(database, initial):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        with pytest.raises(StaleAgentRunStatusError) as exc:
            await AgentService(factory, runner).execute_run(request.run_id)
        assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, initial)
        runner.run.assert_not_awaited()
        await _assert_persisted(factory, request, workspace_id, initial)

    database(check, initial=initial)


@pytest.mark.parametrize("failure", ["runner", "unexpected", "result-type", "outcome"])
def test_agent_service_saves_failed_and_propagates_execution_error(database, failure):
    async def check(factory, request, workspace_id):
        if failure in ("runner", "unexpected"):
            error = AgentRunnerError("execution_failed") if failure == "runner" else RuntimeError("synthetic failure")
            run = AsyncMock(side_effect=error)
        else:
            error = AgentRunnerError("invalid_runner_result")
            invalid = object() if failure == "result-type" else AgentFinalResult.model_construct(status="unsupported")
            run = AsyncMock(return_value=invalid)

        with pytest.raises(type(error)) as exc:
            await AgentService(factory, SimpleNamespace(run=run)).execute_run(request.run_id)
        if failure in ("runner", "unexpected"):
            assert exc.value is error
        else:
            assert exc.value.reason == "invalid_runner_result"
        run.assert_awaited_once_with(request)
        code = (
            AgentFailureCode.EXECUTION_FAILED if failure in ("runner", "unexpected")
            else AgentFailureCode.INVALID_RUNNER_RESULT
        )
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED), failure=code,
        )

    database(check)


async def _cancel_and_join(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)


def test_agent_service_concurrent_claim(database, monkeypatch):
    async def check(factory, request, workspace_id):
        first_locked, second_entered = asyncio.Event(), asyncio.Event()
        release_claim = asyncio.Event()
        runner_started, release_runner = asyncio.Event(), asyncio.Event()
        pids = []
        original_lock = AgentRunRepository.get_for_update

        async def observe_claim(repository, run_id):
            pid = await repository.session.scalar(text("SELECT pg_backend_pid()"))
            pids.append(pid)
            if len(pids) == 1:
                run = await original_lock(repository, run_id)
                first_locked.set()
                await release_claim.wait()
                return run
            if len(pids) == 2:
                second_entered.set()
            return await original_lock(repository, run_id)

        async def run(snapshot):
            assert snapshot == request
            runner_started.set()
            await release_runner.wait()
            return expected

        monkeypatch.setattr(AgentRunRepository, "get_for_update", observe_claim)
        expected = _result()
        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner)
        tasks = [asyncio.create_task(service.execute_run(request.run_id))]
        try:
            async with asyncio.timeout(10):
                await first_locked.wait()
                tasks.append(asyncio.create_task(service.execute_run(request.run_id)))
                await second_entered.wait()
                first_pid, second_pid = pids
                assert first_pid != second_pid
                async with factory() as observer:
                    while first_pid not in await observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                    ):
                        assert not tasks[1].done(), "Second claim did not wait for the first row lock"
                assert not tasks[0].done() and not tasks[1].done()
                runner.run.assert_not_awaited()
                release_claim.set()
                await runner_started.wait()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await tasks[1]
                assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.RUNNING)
                assert not tasks[0].done()
                await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                release_runner.set()
                assert await tasks[0] is expected
        finally:
            release_claim.set()
            release_runner.set()
            await _cancel_and_join(*tasks)

        runner.run.assert_awaited_once_with(request)
        await _assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING), result=expected
        )

    database(check)


@pytest.mark.parametrize("outcome", ["ready", "blocked", "error"])
def test_agent_service_rejects_late_result(database, outcome):
    async def check(factory, request, workspace_id):
        started, release = asyncio.Event(), asyncio.Event()
        error = AgentRunnerError("execution_failed")

        async def run(snapshot):
            assert snapshot == request
            started.set()
            await release.wait()
            if outcome == "error":
                raise error
            return _result(blocker="Нужны данные" if outcome == "blocked" else None)

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        task = asyncio.create_task(AgentService(factory, runner).execute_run(request.run_id))
        cancelled = (Status.RUNNING, Status.CANCELLED)
        try:
            async with asyncio.timeout(10):
                await started.wait()
                async with factory() as session:
                    await AgentRunService(session).transition_status(request.run_id, *cancelled)
                assert not task.done()
                await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)
                release.set()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await task
        finally:
            release.set()
            await _cancel_and_join(task)

        assert (exc.value.expected, exc.value.actual) == cancelled
        assert exc.value.__cause__ is (error if outcome == "error" else None)
        runner.run.assert_awaited_once_with(request)
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)

    database(check)


@pytest.mark.parametrize("phase", ["claim", "finish"])
def test_agent_service_rolls_back_status_and_event_on_constraint_failure(database, monkeypatch, phase):
    async def check(factory, request, workspace_id):
        original_append = AgentEventRepository.add_status_changed
        failed_target = Status.RUNNING if phase == "claim" else Status.VERIFYING
        appended = []

        async def append_then_break_constraint(repository, run_id, current, target):
            appended.append((current, target))
            event = await original_append(repository, run_id, current, target)
            if target is failed_target:
                session = repository.session
                # Flush both writes before provoking a PostgreSQL CHECK violation.
                await session.flush()
                assert await session.scalar(
                    select(AgentRunModel.status).where(AgentRunModel.id == run_id)
                ) == target.value
                assert await session.scalar(
                    select(AgentEventModel.sequence).where(AgentEventModel.id == event.id)
                ) == event.sequence
                event.sequence = 0
                await session.flush()
            return event

        monkeypatch.setattr(AgentEventRepository, "add_status_changed", append_then_break_constraint)
        runner = SimpleNamespace(run=AsyncMock(return_value=_result()))
        with pytest.raises(IntegrityError) as exc:
            await AgentService(factory, runner).execute_run(request.run_id)

        assert exc.value.orig.sqlstate == "23514"
        if phase == "claim":
            assert appended == [CLAIM]
            runner.run.assert_not_awaited()
            await _assert_persisted(factory, request, workspace_id, Status.QUEUED)
        else:
            assert appended == [CLAIM, (Status.RUNNING, Status.VERIFYING)]
            runner.run.assert_awaited_once_with(request)
            await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)

    database(check)


def test_agent_service_propagates_coroutine_cancellation_without_transition(database):
    async def check(factory, request, workspace_id):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(snapshot):
            assert snapshot == request
            started.set()
            await release.wait()
            return _result()

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        task = asyncio.create_task(AgentService(factory, runner).execute_run(request.run_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert task.cancelled()
        finally:
            release.set()
            await _cancel_and_join(task)

        runner.run.assert_awaited_once_with(request)
        await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)

    database(check)


def test_agent_service_executes_real_graph(database, monkeypatch):
    async def check(factory, request, workspace_id):
        original = read_fixture.coroutine
        execution = AsyncMock(wraps=original)

        @wraps(original)
        async def observe_fixture(**kwargs):
            return await execution(**kwargs)

        monkeypatch.setattr(read_fixture, "coroutine", observe_fixture)
        research = create_autospec(Runnable, instance=True)
        research.ainvoke.side_effect = [
            AIMessage(content="", tool_calls=[{
                "name": "read_fixture", "id": "fixture-1", "args": {"component": "task_service"},
            }]),
            AIMessage(content="Синтетический fixture прочитан."),
        ]
        finalizer = create_autospec(Runnable, instance=True)
        finalizer.ainvoke.return_value = {
            "raw": AIMessage(content=""),
            "parsed": TaskAnalysis(
                summary="Предварительный анализ", relevant_components=[], assumptions=[], blocked_reason=None
            ),
            "parsing_error": None,
        }
        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))

        result = await AgentService(factory, runner).execute_run(request.run_id)

        assert result == _result(observations=(
            FixtureObservation(component="task_service", tool_call_id="fixture-1", content=FIXTURE_TEXT),
        ))
        assert research.ainvoke.await_count == 2
        execution.assert_awaited_once_with(component="task_service")
        finalizer.ainvoke.assert_awaited_once()
        await _assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING), result=result
        )

    database(check)


@pytest.mark.parametrize("phase", ["research", "finalization"])
def test_agent_service_deadline_cancels_graph(database, monkeypatch, phase):
    async def check(factory, request, workspace_id):
        waiting = asyncio.Event()
        started, cancelled, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def wait_for_model(*_args, **_kwargs):
            started.set()
            try:
                await waiting.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                cleaned.set()

        original = read_fixture.coroutine
        execution = AsyncMock(wraps=original)

        @wraps(original)
        async def observe_fixture(**kwargs):
            return await execution(**kwargs)

        monkeypatch.setattr(read_fixture, "coroutine", observe_fixture)
        research = create_autospec(Runnable, instance=True)
        finalizer = create_autospec(Runnable, instance=True)
        if phase == "research":
            research.ainvoke.side_effect = wait_for_model
        else:
            research.ainvoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "read_fixture", "id": "fixture-1", "args": {"component": "task_service"},
                }]),
                AIMessage(content="Синтетический fixture прочитан."),
            ]
            finalizer.ainvoke.side_effect = wait_for_model

        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))
        run = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", run)
        service = AgentService(factory, runner, execution_timeout_seconds=0.5)
        async with asyncio.timeout(5):
            with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$") as exc:
                await service.execute_run(request.run_id)
            await asyncio.wait_for(cleaned.wait(), timeout=2)

        assert exc.value.reason == "execution_deadline_exceeded"
        assert isinstance(exc.value.__cause__, TimeoutError)
        assert started.is_set() and cancelled.is_set()
        run.assert_awaited_once_with(request)
        if phase == "research":
            research.ainvoke.assert_awaited_once()
            execution.assert_not_awaited()
            finalizer.ainvoke.assert_not_awaited()
        else:
            assert research.ainvoke.await_count == 2
            execution.assert_awaited_once_with(component="task_service")
            finalizer.ainvoke.assert_awaited_once()
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED),
            failure=AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED,
        )

    database(check)


def test_agent_service_deadline_preserves_domain_cancellation(database):
    async def check(factory, request, workspace_id):
        started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(_snapshot):
            started.set()
            try:
                await release.wait()
                return _result()
            finally:
                cleaned.set()

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner, execution_timeout_seconds=2)
        task = asyncio.create_task(service.execute_run(request.run_id))
        cancelled = (Status.RUNNING, Status.CANCELLED)
        try:
            async with asyncio.timeout(8):
                await started.wait()
                await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                async with factory() as session:
                    await AgentRunService(session).transition_status(request.run_id, *cancelled)
                await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)
                assert not cleaned.is_set() and not task.done()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await task
                assert (exc.value.expected, exc.value.actual) == cancelled
                deadline_error = exc.value.__cause__
                assert isinstance(deadline_error, AgentRunnerError)
                assert deadline_error.reason == "execution_deadline_exceeded"
                assert isinstance(deadline_error.__cause__, TimeoutError)
                assert cleaned.is_set()
        finally:
            release.set()
            await _cancel_and_join(task)

        runner.run.assert_awaited_once_with(request)
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)

    database(check)


@pytest.mark.parametrize("error,code", [
    (AgentRunnerError("provider_timeout"), AgentFailureCode.PROVIDER_TIMEOUT),
    (AgentRunnerError("provider_quota_exhausted"), AgentFailureCode.PROVIDER_QUOTA_EXHAUSTED),
    (TimeoutError("synthetic private detail"), AgentFailureCode.RUNNER_TIMEOUT),
    (AgentRunnerError("synthetic private detail"), AgentFailureCode.EXECUTION_FAILED),
])
def test_agent_service_persists_classified_execution_failure(database, error, code):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock(side_effect=error))
        service = AgentService(factory, runner)
        with pytest.raises(type(error)) as exc:
            await service.execute_run(request.run_id)
        assert exc.value is error
        runner.run.assert_awaited_once_with(request)
        assert await service.get_run_failure(request.run_id) == AgentFailure(code)
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED), failure=code,
        )

    database(check)


def test_agent_service_failure_persists_across_engines(database):
    async def check(_factory, request, workspace_id):
        settings = Settings(postgres_db=os.environ["FORGE_TASK06_TEST_DB"])
        error = AgentRunnerError("provider_connection")
        runner = SimpleNamespace(run=AsyncMock(side_effect=error))
        writer_engine = create_engine(settings)
        try:
            writer = AgentService(create_session_factory(writer_engine), runner)
            with pytest.raises(AgentRunnerError) as exc:
                await writer.execute_run(request.run_id)
            assert exc.value is error
            runner.run.assert_awaited_once_with(request)
        finally:
            await asyncio.wait_for(writer_engine.dispose(), timeout=5)

        reader_engine = create_engine(settings)
        try:
            factory = create_session_factory(reader_engine)
            unused_runner = SimpleNamespace(run=AsyncMock())
            reader = AgentService(factory, unused_runner)
            failure = await reader.get_run_failure(request.run_id)
            assert failure == AgentFailure(AgentFailureCode.PROVIDER_CONNECTION)
            assert failure.retry_disposition is RetryDisposition.TRANSIENT_CANDIDATE
            unused_runner.run.assert_not_called()
            await _assert_persisted(
                factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED),
                failure=AgentFailureCode.PROVIDER_CONNECTION,
            )
        finally:
            await asyncio.wait_for(reader_engine.dispose(), timeout=5)

    database(check)


@pytest.mark.parametrize("failure_type", ["agent_execution_failed", "agent_run_status_changed"])
def test_agent_service_failure_is_atomic(database, monkeypatch, failure_type):
    async def check(factory, request, workspace_id):
        original_append = AgentEventRepository.append
        attempted = []
        persistence_errors = []
        execution_error = RuntimeError("synthetic private detail")
        expected = [
            ("agent_run_status_changed", {"from": "queued", "to": "running"}),
            ("agent_execution_failed", {"schema_version": 1, "code": "execution_failed"}),
        ]
        if failure_type == "agent_run_status_changed":
            expected.append(("agent_run_status_changed", {"from": "running", "to": "failed"}))

        async def append_then_break(repository, run_id, *, event_type, payload):
            attempted.append((event_type, payload))
            event = await original_append(repository, run_id, event_type=event_type, payload=payload)
            if len(attempted) == 1 or event_type != failure_type:
                return event
            session = repository.session
            await session.flush()
            status, result_is_null = (await session.execute(
                select(AgentRunModel.status, AgentRunModel.final_result.is_(None)).where(AgentRunModel.id == run_id)
            )).one()
            expected_status = "running" if failure_type == "agent_execution_failed" else "failed"
            assert (status, result_is_null) == (expected_status, True)
            events = (await session.execute(
                select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload)
                .where(AgentEventModel.agent_run_id == run_id).order_by(AgentEventModel.sequence)
            )).all()
            assert events == [(number, *item) for number, item in enumerate(expected, 1)]
            event.sequence = 0
            try:
                await session.flush()
            except IntegrityError as error:
                persistence_errors.append(error)
                raise

        monkeypatch.setattr(AgentEventRepository, "append", append_then_break)
        runner = SimpleNamespace(run=AsyncMock(side_effect=execution_error))
        service = AgentService(factory, runner)
        with pytest.raises(IntegrityError) as exc:
            await service.execute_run(request.run_id)
        assert persistence_errors == [exc.value]
        assert exc.value.orig.sqlstate == "23514"
        assert exc.value.orig.__cause__.constraint_name == "ck_agent_events_sequence_positive"
        assert exc.value.__cause__ is execution_error
        assert attempted == expected
        runner.run.assert_awaited_once_with(request)
        assert await service.get_run_failure(request.run_id) is None
        await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)

    database(check)


def test_agent_service_serializes_concurrent_failures(database, monkeypatch):
    async def check(factory, request, workspace_id):
        winner = AgentFailure(AgentFailureCode.PROVIDER_TIMEOUT)
        loser = AgentFailure(AgentFailureCode.RUNNER_TIMEOUT)
        staged, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async with factory() as session:
            assert await AgentRunService(session).claim_for_execution(request.run_id) == request

        async with factory() as first, factory() as second:
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid != second_pid
            cached_run = await second.get(AgentRunModel, request.run_id)
            assert cached_run.status == "running"
            first_service = AgentRunService(first)
            original = first_service.events.add_status_changed

            async def stage_and_wait(*args):
                event = await original(*args)
                await first.flush()
                staged.set()
                await release.wait()
                return event

            async def compete():
                entered.set()
                await AgentRunService(second).fail_execution(request.run_id, loser)

            monkeypatch.setattr(first_service.events, "add_status_changed", stage_and_wait)
            tasks = [asyncio.create_task(first_service.fail_execution(request.run_id, winner))]
            try:
                async with asyncio.timeout(10):
                    await staged.wait()
                    tasks.append(asyncio.create_task(compete()))
                    await entered.wait()
                    async with factory() as observer:
                        while first_pid not in await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                        ):
                            assert not tasks[1].done(), "Second failure did not wait on the run lock"
                            await asyncio.sleep(0.01)
                    await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                    release.set()
                    await tasks[0]
                    with pytest.raises(StaleAgentRunStatusError) as exc:
                        await tasks[1]
                    assert (exc.value.expected, exc.value.actual) == (Status.RUNNING, Status.FAILED)
            finally:
                release.set()
                await _cancel_and_join(*tasks)

        async with factory() as session:
            with pytest.raises(StaleAgentRunStatusError) as exc:
                await AgentRunService(session).fail_execution(request.run_id, loser)
            assert (exc.value.expected, exc.value.actual) == (Status.RUNNING, Status.FAILED)
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED), failure=winner.code,
        )

    database(check)


def test_agent_service_failure_rejects_existing_metadata_on_running_run(database):
    async def check(factory, request, workspace_id):
        event_payload = {"schema_version": 1, "code": "runner_timeout"}
        async with factory.begin() as session:
            await AgentEventRepository(session).append(
                request.run_id, event_type="agent_execution_failed", payload=event_payload,
            )
        async with factory() as session:
            service = AgentRunService(session)
            with pytest.raises(AgentFailureContractError, match="^duplicate_execution_failure$"):
                await service.fail_execution(request.run_id, AgentFailure(AgentFailureCode.EXECUTION_FAILED))
            assert not session.in_transaction()
            assert await service.get_failure(request.run_id) == AgentFailure(AgentFailureCode.RUNNER_TIMEOUT)
            events = await service.events.get_execution_failure_events(request.run_id)
            assert [(event.sequence, event.payload) for event in events] == [(1, event_payload)]
            run = await service.runs.get(request.run_id)
            assert run.status == "running" and run.final_result is None

    database(check, initial=Status.RUNNING)


def test_agent_service_failure_rejects_running_run_with_result(database):
    async def check(factory, request, workspace_id):
        result = _result()
        async with factory.begin() as session:
            run = await session.get(AgentRunModel, request.run_id)
            run.final_result = encode_agent_result(result)
        async with factory() as session:
            service = AgentRunService(session)
            with pytest.raises(AgentRunResultAlreadyRecordedError, match="^result_already_recorded$"):
                await service.fail_execution(request.run_id, AgentFailure(AgentFailureCode.EXECUTION_FAILED))
            assert not session.in_transaction()
        # No lifecycle events were added, and the existing result survives.
        async with factory() as session:
            run = await session.get(AgentRunModel, request.run_id)
            assert run.status == "running" and run.final_result == encode_agent_result(result)
            assert not (await session.scalars(
                select(AgentEventModel).where(AgentEventModel.agent_run_id == request.run_id)
            )).all()

    database(check, initial=Status.RUNNING)


@pytest.mark.parametrize("case", ["missing-run", "historical-failed", "blocked", "ready"])
def test_agent_service_reads_absent_failure_metadata(database, case):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        service = AgentService(factory, runner)
        if case == "missing-run":
            with pytest.raises(AgentRunNotFoundError):
                await service.get_run_failure(uuid4())
            await _assert_persisted(factory, request, workspace_id, Status.QUEUED)
        elif case == "historical-failed":
            assert await service.get_run_failure(request.run_id) is None
            await _assert_persisted(factory, request, workspace_id, Status.FAILED)
        else:
            result = _result(blocker="Synthetic blocker" if case == "blocked" else None)
            target = Status.FAILED if case == "blocked" else Status.VERIFYING
            async with factory() as session:
                runs = AgentRunService(session)
                await runs.claim_for_execution(request.run_id)
                await runs.complete_execution(request.run_id, result)
            assert await service.get_run_failure(request.run_id) is None
            await _assert_persisted(
                factory, request, workspace_id, target, CLAIM, (Status.RUNNING, target), result=result,
            )
        runner.run.assert_not_called()

    database(check, initial=Status.FAILED if case == "historical-failed" else Status.QUEUED)


@pytest.mark.parametrize("payloads,reason", [
    ([{"schema_version": 1, "code": "synthetic private detail"}], "invalid_stored_failure"),
    ([{"schema_version": 2, "code": "runner_timeout"}], "unsupported_failure_version"),
    ([{"schema_version": 1, "code": "runner_timeout"}] * 2, "duplicate_execution_failure"),
])
def test_agent_service_rejects_invalid_failure_history(database, payloads, reason):
    async def check(factory, request, _workspace_id):
        async with factory.begin() as session:
            repository = AgentEventRepository(session)
            for payload in payloads:
                await repository.append(request.run_id, event_type="agent_execution_failed", payload=payload)
        runner = SimpleNamespace(run=AsyncMock())
        with pytest.raises(AgentFailureContractError, match=f"^{reason}$"):
            await AgentService(factory, runner).get_run_failure(request.run_id)
        runner.run.assert_not_called()
        async with factory() as session:
            events = await AgentEventRepository(session).get_execution_failure_events(request.run_id)
            assert [(event.sequence, event.payload) for event in events] == list(enumerate(payloads, 1))

    database(check, initial=Status.FAILED)
