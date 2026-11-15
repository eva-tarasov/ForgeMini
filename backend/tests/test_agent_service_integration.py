"""Opt-in lifecycle tests against a dedicated, already migrated PostgreSQL database."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langsmith import tracing_context
from model_test_support import TRACING_OFF
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.agent_runtime.agent_graph import create_agent_graph
from app.agent_runtime.fixture_tools import read_fixture
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.structured_output import TaskAnalysis
from app.agent_runtime.usage_capture import invoke_observed_model
from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus as Status
from app.domain.transitions import InvalidAgentRunTransition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.active_runs import ActiveRunRegistry
from app.services.agent_cost import AgentPricing, CostStatus, ModelTariff
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
from app.services.agent_service import AgentService, AgentUsagePersistenceError
from app.services.agent_usage import (
    AgentUsageSnapshot,
    ModelCallOutcome,
    ModelCallStage,
    ModelCallUsage,
    TokenUsage,
    UsageStatus,
)
from app.services.agent_usage_codec import AgentUsageContractError, AgentUsageRecord, encode_agent_usage


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


async def _assert_persisted(factory, request, workspace_id, status, *edges, result=None, failure=None, usage=None):
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
            target = Status.VERIFYING if result.status == "ready_for_verification" else Status.FAILED
            index = edges.index((Status.RUNNING, target))
            expected.insert(index, ("agent_result_recorded", {"schema_version": 1, "result_status": result.status}))
        if failure is not None:
            expected.insert(-1, ("agent_execution_failed", {"schema_version": 1, "code": failure.value}))
        if usage is not None:
            expected.append(("agent_usage_recorded", encode_agent_usage(usage)))
        assert [
            (event.sequence, event.event_type, event.payload) for event in events
        ] == [
            (number, event_type, payload) for number, (event_type, payload) in enumerate(expected, 1)
        ]


CLAIM = (Status.QUEUED, Status.RUNNING)


def _usage_record(request, *calls, pricing=AgentPricing()):
    return AgentUsageRecord(AgentUsageSnapshot(request.run_id, calls), pricing)


def _unknown_graph_usage(request, *, cancelled_phase=None):
    stages = (ModelCallStage.RESEARCH, ModelCallStage.RESEARCH, ModelCallStage.FINALIZATION)
    if cancelled_phase == "research":
        stages = stages[:1]
    return _usage_record(request, *(
        ModelCallUsage(
            number, stage,
            ModelCallOutcome.CANCELLED if cancelled_phase and number == len(stages) else ModelCallOutcome.RETURNED,
            UsageStatus.MISSING, None,
        )
        for number, stage in enumerate(stages, 1)
    ))


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
            factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING),
            result=result, usage=_unknown_graph_usage(request),
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
            usage=_unknown_graph_usage(request, cancelled_phase=phase),
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


@pytest.mark.parametrize("initial", list(Status))
def test_agent_service_cancels_from_supported_states(database, initial):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        service = AgentService(factory, runner)
        edges = ()
        result = None
        if initial is Status.VERIFYING:
            result = _result()
            async with factory() as session:
                runs = AgentRunService(session)
                await runs.claim_for_execution(request.run_id)
                await runs.complete_execution(request.run_id, result)
            edges = (CLAIM, (Status.RUNNING, Status.VERIFYING))
        if initial in (Status.FAILED, Status.SUCCEEDED):
            with pytest.raises(InvalidAgentRunTransition) as exc:
                await service.cancel_run(request.run_id)
            assert (exc.value.current, exc.value.target) == (initial, Status.CANCELLED)
            await _assert_persisted(factory, request, workspace_id, initial)
        else:
            await service.cancel_run(request.run_id)
            await service.cancel_run(request.run_id)
            if initial is not Status.CANCELLED:
                edges += ((initial, Status.CANCELLED),)
            await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, *edges, result=result)
            if initial is Status.QUEUED:
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await service.execute_run(request.run_id)
                assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.CANCELLED)
                await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, *edges)
            assert await service.get_run_failure(request.run_id) is None
        runner.run.assert_not_called()

    database(check, initial=Status.QUEUED if initial is Status.VERIFYING else initial)


def test_agent_service_rejects_missing_cancellation_target(database):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        with pytest.raises(AgentRunNotFoundError):
            await AgentService(factory, runner).cancel_run(uuid4())
        await _assert_persisted(factory, request, workspace_id, Status.QUEUED)
        runner.run.assert_not_called()

    database(check)


@pytest.mark.parametrize("phase", ["research", "finalization"])
def test_agent_service_cancel_stops_active_graph(database, monkeypatch, phase):
    async def check(factory, request, workspace_id):
        started, cleaned = asyncio.Event(), asyncio.Event()
        observed_statuses = []

        async def wait_for_model(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                async with factory() as session:
                    status = await session.scalar(
                        select(AgentRunModel.status).where(AgentRunModel.id == request.run_id)
                    )
                    observed_statuses.append(status)
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
        registry = ActiveRunRegistry()
        service = AgentService(factory, runner, active_runs=registry, execution_timeout_seconds=60)
        tasks = [asyncio.create_task(service.execute_run(request.run_id))]
        cancelled = (Status.RUNNING, Status.CANCELLED)
        try:
            # This outer bound is far shorter than the runner deadline.
            async with asyncio.timeout(8):
                await started.wait()
                await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                tasks.append(asyncio.create_task(service.cancel_run(request.run_id)))
                await tasks[1]
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                await cleaned.wait()
            assert tasks[0].cancelled() and observed_statuses == ["cancelled"]
        finally:
            await _cancel_and_join(*tasks)

        run.assert_awaited_once_with(request)
        if phase == "research":
            research.ainvoke.assert_awaited_once()
            execution.assert_not_awaited()
            finalizer.ainvoke.assert_not_awaited()
        else:
            assert research.ainvoke.await_count == 2
            execution.assert_awaited_once_with(component="task_service")
            finalizer.ainvoke.assert_awaited_once()
        assert request.run_id not in registry
        assert await service.get_run_failure(request.run_id) is None
        await _assert_persisted(
            factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled,
            usage=_unknown_graph_usage(request, cancelled_phase=phase),
        )

    database(check)


def test_agent_service_cancel_during_claim_cleanup(database, monkeypatch):
    async def check(factory, request, workspace_id):
        committed, release = asyncio.Event(), asyncio.Event()
        runner = SimpleNamespace(run=AsyncMock())
        registry = ActiveRunRegistry()
        service = AgentService(factory, runner, active_runs=registry)
        original = AgentRunService.claim_for_execution

        async def hold_committed_claim(runs, run_id):
            assert run_id in registry
            snapshot = await original(runs, run_id)
            assert not runs.session.in_transaction()
            committed.set()
            await release.wait()
            return snapshot

        monkeypatch.setattr(AgentRunService, "claim_for_execution", hold_committed_claim)
        tasks = [asyncio.create_task(service.execute_run(request.run_id))]
        cancelled = (Status.RUNNING, Status.CANCELLED)
        try:
            async with asyncio.timeout(8):
                await committed.wait()
                await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                tasks.append(asyncio.create_task(service.cancel_run(request.run_id)))
                await tasks[1]
                await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)
                assert not tasks[0].done() and tasks[0].cancelling() == 0
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
        finally:
            release.set()
            await _cancel_and_join(*tasks)

        runner.run.assert_not_called()
        assert request.run_id not in registry
        assert await service.get_run_failure(request.run_id) is None
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled)

    database(check)


async def _wait_for_run_blocker(factory, holder_pid, waiter_pid, waiter):
    assert holder_pid != waiter_pid
    async with factory() as observer:
        while holder_pid not in await observer.scalar(
            text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
        ):
            assert not waiter.done(), "Expected a wait on the AgentRun row lock"
            await asyncio.sleep(0.01)  # Poll PostgreSQL for evidence, independent of task ordering.


def test_agent_service_cancellation_wins_before_claim(database, monkeypatch):
    async def check(factory, request, workspace_id):
        staged, release, contender_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pids = []
        registry = ActiveRunRegistry()
        runner = SimpleNamespace(run=AsyncMock())
        service = AgentService(factory, runner, active_runs=registry)
        original_lock = AgentRunRepository.get_for_update
        original_append = AgentEventRepository.add_status_changed

        async def track_lock(repository, run_id):
            pids.append(await repository.session.scalar(text("SELECT pg_backend_pid()")))
            if len(pids) == 2:
                assert run_id in registry
                contender_entered.set()
            return await original_lock(repository, run_id)

        async def hold_cancellation(repository, run_id, current, target):
            event = await original_append(repository, run_id, current, target)
            if target is Status.CANCELLED:
                await repository.session.flush()
                staged.set()
                await release.wait()
            return event

        monkeypatch.setattr(AgentRunRepository, "get_for_update", track_lock)
        monkeypatch.setattr(AgentEventRepository, "add_status_changed", hold_cancellation)
        tasks = [asyncio.create_task(service.cancel_run(request.run_id))]
        try:
            async with asyncio.timeout(8):
                await staged.wait()
                execution = asyncio.create_task(service.execute_run(request.run_id))
                tasks.append(execution)
                await contender_entered.wait()
                await _wait_for_run_blocker(factory, *pids, execution)
                await _assert_persisted(factory, request, workspace_id, Status.QUEUED)
                release.set()
                await tasks[0]
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await execution
                assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.CANCELLED)
                assert execution.cancelling() == 0
        finally:
            release.set()
            await _cancel_and_join(*tasks)

        runner.run.assert_not_called()
        assert request.run_id not in registry
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, (Status.QUEUED, Status.CANCELLED))

    database(check)


async def _exercise_cancel_persistence_race(
    factory, request, workspace_id, monkeypatch, *, cancellation_first, execution_fails,
):
    started, release_runner = asyncio.Event(), asyncio.Event()
    first_staged, release_first = asyncio.Event(), asyncio.Event()
    second_entered, second_locked, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}
    result = _result()
    error = AgentRunnerError("provider_connection")
    execution_target = Status.FAILED if execution_fails else Status.VERIFYING
    first_target = Status.CANCELLED if cancellation_first else execution_target

    async def run(_snapshot):
        started.set()
        await release_runner.wait()
        if execution_fails:
            raise error
        return result

    original_lock = AgentRunRepository.get_for_update
    original_append = AgentEventRepository.add_status_changed

    async def track_lock(repository, run_id):
        task = asyncio.current_task()
        pids[task] = await repository.session.scalar(text("SELECT pg_backend_pid()"))
        if len(pids) == 2:
            second_entered.set()
            row = await original_lock(repository, run_id)
            second_locked.set()
            await release_second.wait()
            return row
        return await original_lock(repository, run_id)

    async def stage_and_hold(repository, run_id, current, target):
        event = await original_append(repository, run_id, current, target)
        if target is first_target:
            await repository.session.flush()
            first_staged.set()
            await release_first.wait()
        return event

    runner = SimpleNamespace(run=AsyncMock(side_effect=run))
    registry = ActiveRunRegistry()
    service = AgentService(factory, runner, active_runs=registry)
    execution = asyncio.create_task(service.execute_run(request.run_id))
    tasks = [execution]
    cancelled_edge = (Status.RUNNING, Status.CANCELLED)
    try:
        async with asyncio.timeout(10):
            await started.wait()
            monkeypatch.setattr(AgentRunRepository, "get_for_update", track_lock)
            monkeypatch.setattr(AgentEventRepository, "add_status_changed", stage_and_hold)
            if cancellation_first:
                cancellation = asyncio.create_task(service.cancel_run(request.run_id))
                tasks.append(cancellation)
                first, second = cancellation, execution
                await first_staged.wait()
                release_runner.set()
            else:
                release_runner.set()
                await first_staged.wait()
                cancellation = asyncio.create_task(service.cancel_run(request.run_id))
                tasks.append(cancellation)
                first, second = execution, cancellation

            await second_entered.wait()
            await _wait_for_run_blocker(factory, pids[first], pids[second], second)
            await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
            release_first.set()
            if cancellation_first:
                await cancellation
                await second_locked.wait()
                assert execution.cancelling() == 0 and not execution.done()
                release_second.set()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await execution
                assert (exc.value.expected, exc.value.actual) == cancelled_edge
                assert exc.value.__cause__ is (error if execution_fails else None)
            elif execution_fails:
                with pytest.raises(AgentRunnerError) as exc:
                    await execution
                assert exc.value is error
                release_second.set()
                with pytest.raises(InvalidAgentRunTransition) as exc:
                    await cancellation
                assert (exc.value.current, exc.value.target) == (Status.FAILED, Status.CANCELLED)
            else:
                assert await execution is result
                release_second.set()
                await cancellation
    finally:
        release_runner.set()
        release_first.set()
        release_second.set()
        await _cancel_and_join(*tasks)

    runner.run.assert_awaited_once_with(request)
    assert request.run_id not in registry
    if cancellation_first:
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancelled_edge)
    elif execution_fails:
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED),
            failure=AgentFailureCode.PROVIDER_CONNECTION,
        )
    else:
        await _assert_persisted(
            factory, request, workspace_id, Status.CANCELLED, CLAIM,
            (Status.RUNNING, Status.VERIFYING), (Status.VERIFYING, Status.CANCELLED), result=result,
        )


@pytest.mark.parametrize("cancellation_first", [True, False], ids=["cancel-first", "completion-first"])
def test_agent_service_cancel_completion_race(database, monkeypatch, cancellation_first):
    async def check(factory, request, workspace_id):
        await _exercise_cancel_persistence_race(
            factory, request, workspace_id, monkeypatch,
            cancellation_first=cancellation_first, execution_fails=False,
        )

    database(check)


@pytest.mark.parametrize("cancellation_first", [True, False], ids=["cancel-first", "failure-first"])
def test_agent_service_cancel_failure_race(database, monkeypatch, cancellation_first):
    async def check(factory, request, workspace_id):
        await _exercise_cancel_persistence_race(
            factory, request, workspace_id, monkeypatch,
            cancellation_first=cancellation_first, execution_fails=True,
        )

    database(check)


def test_agent_service_cancel_rollback_leaves_runner_active(database, monkeypatch):
    async def check(factory, request, workspace_id):
        started, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        result = _result()

        async def run(_snapshot):
            started.set()
            try:
                await release.wait()
                return result
            except asyncio.CancelledError:
                interrupted.set()
                raise

        original_append = AgentEventRepository.add_status_changed

        async def invalidate_event(repository, run_id, current, target):
            event = await original_append(repository, run_id, current, target)
            if target is Status.CANCELLED:
                session = repository.session
                await session.flush()
                assert await session.scalar(
                    select(AgentRunModel.status).where(AgentRunModel.id == run_id)
                ) == "cancelled"
                assert await session.scalar(
                    select(AgentEventModel.sequence).where(AgentEventModel.id == event.id)
                ) == 2
                event.sequence = 0
                await session.flush()
            return event

        monkeypatch.setattr(AgentEventRepository, "add_status_changed", invalidate_event)
        registry = ActiveRunRegistry()
        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner, active_runs=registry)
        task = asyncio.create_task(service.execute_run(request.run_id))
        try:
            async with asyncio.timeout(8):
                await started.wait()
                with pytest.raises(IntegrityError) as exc:
                    await service.cancel_run(request.run_id)
                assert exc.value.orig.sqlstate == "23514"
                assert exc.value.orig.__cause__.constraint_name == "ck_agent_events_sequence_positive"
                assert not task.done() and task.cancelling() == 0 and not interrupted.is_set()
                await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                release.set()
                assert await task is result
        finally:
            release.set()
            await _cancel_and_join(task)

        runner.run.assert_awaited_once_with(request)
        assert request.run_id not in registry
        await _assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING), result=result,
        )

    database(check)


def test_agent_service_serializes_concurrent_cancellation_and_signals_once(database, monkeypatch):
    async def check(factory, request, workspace_id):
        started, signalled = asyncio.Event(), asyncio.Event()
        release_runner, first_staged, release_first, second_entered = (
            asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event(),
        )
        pids = []

        async def run(_snapshot):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                signalled.set()
                await release_runner.wait()
                raise

        registry = ActiveRunRegistry()
        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner, active_runs=registry)
        execution = asyncio.create_task(service.execute_run(request.run_id))
        tasks = [execution]
        original_lock = AgentRunRepository.get_for_update
        original_append = AgentEventRepository.add_status_changed

        async def track_lock(repository, run_id):
            pids.append(await repository.session.scalar(text("SELECT pg_backend_pid()")))
            if len(pids) == 2:
                second_entered.set()
            return await original_lock(repository, run_id)

        async def stage_and_hold(repository, run_id, current, target):
            event = await original_append(repository, run_id, current, target)
            await repository.session.flush()
            first_staged.set()
            await release_first.wait()
            return event

        try:
            async with asyncio.timeout(10):
                await started.wait()
                monkeypatch.setattr(AgentRunRepository, "get_for_update", track_lock)
                monkeypatch.setattr(AgentEventRepository, "add_status_changed", stage_and_hold)
                tasks.append(asyncio.create_task(service.cancel_run(request.run_id)))
                await first_staged.wait()
                tasks.append(asyncio.create_task(service.cancel_run(request.run_id)))
                await second_entered.wait()
                await _wait_for_run_blocker(factory, *pids, tasks[2])
                assert execution.cancelling() == 0 and not signalled.is_set()
                release_first.set()
                await asyncio.gather(*tasks[1:])
                await signalled.wait()
                await service.cancel_run(request.run_id)
                assert execution.cancelling() == 1 and not execution.done()
                release_runner.set()
                with pytest.raises(asyncio.CancelledError):
                    await execution
        finally:
            release_first.set()
            release_runner.set()
            await _cancel_and_join(*tasks)

        runner.run.assert_awaited_once_with(request)
        assert request.run_id not in registry
        await _assert_persisted(
            factory, request, workspace_id, Status.CANCELLED, CLAIM, (Status.RUNNING, Status.CANCELLED),
        )

    database(check)


TRAINING_TARIFF = ModelTariff("openai", "synthetic-model", "training-v1", Decimal("2.00"), Decimal("8.00"), Decimal("0.50"))
TRAINING_PRICING = AgentPricing(TRAINING_TARIFF, TRAINING_TARIFF)
TRAINING_TOKENS = (TokenUsage(12, 8, 20, 4, 2), TokenUsage(24, 10, 34, 7, 3), TokenUsage(35, 11, 46, 10, 9))


def _reported_usage_call(number, stage, tokens):
    return ModelCallUsage(number, stage, ModelCallOutcome.RETURNED, UsageStatus.REPORTED, tokens)


def _training_usage(request, *, interrupted=False):
    calls = tuple(_reported_usage_call(number, ModelCallStage.RESEARCH, tokens)
                  for number, tokens in enumerate(TRAINING_TOKENS[:2], 1))
    final_stage = ModelCallStage.FINALIZATION
    final = (
        ModelCallUsage(3, final_stage, ModelCallOutcome.CANCELLED, UsageStatus.MISSING, None)
        if interrupted else _reported_usage_call(3, final_stage, TRAINING_TOKENS[2])
    )
    return _usage_record(request, *calls, final, pricing=TRAINING_PRICING)


def _usage_message(content, tokens, **kwargs):
    return AIMessage(content=content, usage_metadata={
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "total_tokens": tokens.total_tokens,
        "input_token_details": {"cache_read": tokens.cached_input_tokens},
        "output_token_details": {"reasoning": tokens.reasoning_tokens},
    }, **kwargs)


def _training_usage_graph(*, blocker=None, failure=None, finalizer_call=None):
    research = create_autospec(Runnable, instance=True)
    research.ainvoke.side_effect = [
        _usage_message("", TRAINING_TOKENS[0], tool_calls=[{
            "name": "read_fixture", "id": "fixture-1", "args": {"component": "task_service"},
        }]),
        _usage_message("Synthetic fixture read.", TRAINING_TOKENS[1]),
    ]
    finalizer = create_autospec(Runnable, instance=True)
    if finalizer_call is None:
        finalizer.ainvoke.return_value = {
            "raw": _usage_message("", TRAINING_TOKENS[2]),
            "parsed": TaskAnalysis(
                summary=" " if failure == "validation" else "Предварительный анализ",
                relevant_components=[], assumptions=[], blocked_reason=blocker,
            ) if failure != "parsing" else None,
            "parsing_error": ValueError("Synthetic private parsing detail") if failure == "parsing" else None,
        }
    else:
        finalizer.ainvoke.side_effect = finalizer_call
    return LangGraphAgentRunner(create_agent_graph(research, finalizer)), research, finalizer


def test_agent_service_usage_persists_across_engines(database, monkeypatch):
    async def check(_factory, request, workspace_id):
        original = read_fixture.coroutine
        fixture = AsyncMock(wraps=original)

        @wraps(original)
        async def observe(**kwargs):
            return await fixture(**kwargs)

        monkeypatch.setattr(read_fixture, "coroutine", observe)
        runner, research, finalizer = _training_usage_graph()
        run = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", run)
        settings = Settings(postgres_db=os.environ["FORGE_TASK06_TEST_DB"])
        writing_engine = create_engine(settings)
        try:
            writer = AgentService(create_session_factory(writing_engine), runner, pricing=TRAINING_PRICING)
            result = await writer.execute_run(request.run_id)
        finally:
            await asyncio.wait_for(writing_engine.dispose(), timeout=5)

        assert result == _result(observations=(
            FixtureObservation(component="task_service", tool_call_id="fixture-1", content=FIXTURE_TEXT),
        ))
        run.assert_awaited_once_with(request)
        fixture.assert_awaited_once_with(component="task_service")
        assert research.ainvoke.await_count == 2
        assert isinstance(research.ainvoke.await_args.args[0][-1], ToolMessage)
        finalizer.ainvoke.assert_awaited_once()

        reading_engine = create_engine(settings)
        try:
            factory = create_session_factory(reading_engine)
            unused_runner = SimpleNamespace(run=AsyncMock())
            reader = AgentService(factory, unused_runner)
            usage = await reader.get_run_usage(request.run_id)
            assert usage == _training_usage(request)
            assert usage.pricing.research.tariff_version == usage.pricing.finalization.tariff_version == "training-v1"
            assert (usage.snapshot.known_input_tokens, usage.snapshot.known_output_tokens, usage.snapshot.known_total_tokens) == (
                71, 29, 100,
            )
            assert usage.cost_estimate.cost_complete and usage.cost_estimate.known_cost_usd == Decimal("0.00034250")
            assert await reader.get_run_result(request.run_id) == result
            unused_runner.run.assert_not_called()
            await _assert_persisted(
                factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING),
                result=result, usage=usage,
            )
        finally:
            await asyncio.wait_for(reading_engine.dispose(), timeout=5)

    database(check)


@pytest.mark.parametrize("case", ["blocked", "parsing", "validation"])
def test_agent_service_usage_survives_blocked_or_failed_graph(database, monkeypatch, case):
    async def check(factory, request, workspace_id):
        runner, research, finalizer = _training_usage_graph(
            blocker="Synthetic blocker" if case == "blocked" else None, failure=case,
        )
        run = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", run)
        service = AgentService(factory, runner, pricing=TRAINING_PRICING)
        if case == "blocked":
            result = await service.execute_run(request.run_id)
            assert result.status == "blocked" and result.blocked_reason == "Synthetic blocker"
            assert await service.get_run_result(request.run_id) == result
            assert await service.get_run_failure(request.run_id) is None
            code = None
        else:
            with pytest.raises(AgentRunnerError, match="^structured_output_invalid$"):
                await service.execute_run(request.run_id)
            result = None
            code = AgentFailureCode.STRUCTURED_OUTPUT_INVALID
            assert await service.get_run_failure(request.run_id) == AgentFailure(code)
        usage = await service.get_run_usage(request.run_id)
        assert usage == _training_usage(request)
        assert usage.cost_estimate.cost_complete and usage.cost_estimate.known_cost_usd == Decimal("0.00034250")
        run.assert_awaited_once_with(request)
        assert research.ainvoke.await_count == 2
        finalizer.ainvoke.assert_awaited_once()
        await _assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED),
            result=result, failure=code, usage=usage,
        )

    database(check)


@pytest.mark.parametrize("interruption", ["explicit", "external", "deadline"])
def test_agent_service_usage_survives_cancellation(database, monkeypatch, interruption):
    async def check(factory, request, workspace_id):
        started, cleaned = asyncio.Event(), asyncio.Event()
        observed_states = []

        async def finalizer_call(_messages):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                async with factory() as session:
                    run = await session.get(AgentRunModel, request.run_id)
                    observed_states.append(run.status)
                raise
            finally:
                cleaned.set()

        runner, research, finalizer = _training_usage_graph(finalizer_call=finalizer_call)
        run = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", run)
        registry = ActiveRunRegistry()
        service = AgentService(
            factory, runner, active_runs=registry, pricing=TRAINING_PRICING,
            execution_timeout_seconds=0.5 if interruption == "deadline" else 60,
        )
        execution = asyncio.create_task(service.execute_run(request.run_id))
        try:
            async with asyncio.timeout(8):
                await started.wait()
                if interruption == "explicit":
                    await service.cancel_run(request.run_id)
                elif interruption == "external":
                    execution.cancel()
                if interruption == "deadline":
                    with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                        await execution
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await execution
                await cleaned.wait()
        finally:
            await _cancel_and_join(execution)

        expected_status = {"explicit": Status.CANCELLED, "external": Status.RUNNING, "deadline": Status.FAILED}
        target = expected_status[interruption]
        code = AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED if interruption == "deadline" else None
        if interruption == "explicit":
            assert observed_states == ["cancelled"]
        assert await service.get_run_failure(request.run_id) == (AgentFailure(code) if code is not None else None)
        assert request.run_id not in registry
        run.assert_awaited_once_with(request)
        assert research.ainvoke.await_count == 2
        finalizer.ainvoke.assert_awaited_once()
        usage = await service.get_run_usage(request.run_id)
        assert usage == _training_usage(request, interrupted=True)
        assert usage.snapshot.known_total_tokens == 54 and not usage.snapshot.usage_complete
        cost = usage.cost_estimate
        assert cost.known_cost_usd == Decimal("0.0001995") and not cost.cost_complete
        assert cost.calls[-1].status is CostStatus.USAGE_UNKNOWN
        edges = (CLAIM,) if target is Status.RUNNING else (CLAIM, (Status.RUNNING, target))
        await _assert_persisted(factory, request, workspace_id, target, *edges, failure=code, usage=usage)

    database(check)


def _fail_event_write(monkeypatch, event_to_fail):
    original = AgentEventRepository.append
    failed_writes = []
    attempted = []

    async def append(repository, run_id, *, event_type, payload):
        attempted.append(event_type)
        event = await original(repository, run_id, event_type=event_type, payload=payload)
        if event_type == event_to_fail:
            session = repository.session
            await session.flush()
            assert await session.scalar(
                select(AgentEventModel.sequence).where(AgentEventModel.id == event.id)
            ) == event.sequence
            event.sequence = 0
            try:
                await session.flush()
            except IntegrityError as error:
                failed_writes.append(error)
                raise
        return event

    monkeypatch.setattr(AgentEventRepository, "append", append)
    return attempted, failed_writes


def _assert_sequence_constraint(error):
    assert error.orig.sqlstate == "23514"
    assert error.orig.__cause__.constraint_name == "ck_agent_events_sequence_positive"


@pytest.mark.parametrize("outcome", ["ready", "failure", "cancelled"])
def test_agent_service_usage_write_failure_preserves_primary_outcome(database, monkeypatch, outcome):
    async def check(factory, request, workspace_id):
        attempted, failed_writes = _fail_event_write(monkeypatch, "agent_usage_recorded")
        model = create_autospec(Runnable, instance=True)
        model.ainvoke.return_value = _usage_message("Synthetic response", TRAINING_TOKENS[0])
        started = asyncio.Event()
        result = _result()
        root = RuntimeError("Synthetic upstream failure")
        original = AgentRunnerError("provider_connection")
        received_cancellation, propagated_cancellation = [], []
        registry = ActiveRunRegistry()

        async def run(_snapshot):
            await invoke_observed_model(model, [], stage=ModelCallStage.RESEARCH)
            if outcome == "failure":
                raise original from root
            if outcome == "cancelled":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    received_cancellation.append(error)
                    raise
            return result

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner, active_runs=registry)

        async def execute():
            try:
                return await service.execute_run(request.run_id)
            except asyncio.CancelledError as error:
                propagated_cancellation.append(error)
                raise

        if outcome == "ready":
            with pytest.raises(AgentUsagePersistenceError, match="^usage_persistence_failed$") as exc:
                await execute()
            assert exc.value.__cause__ is failed_writes[0]
            target, code = Status.VERIFYING, None
            history = ["agent_run_status_changed", "agent_result_recorded", "agent_run_status_changed"]
        elif outcome == "failure":
            with pytest.raises(AgentRunnerError) as exc:
                await execute()
            assert exc.value is original and exc.value.__cause__ is root
            assert exc.value.__notes__ == ["agent_usage_persistence_failed"]
            target, code, result = Status.FAILED, AgentFailureCode.PROVIDER_CONNECTION, None
            history = ["agent_run_status_changed", "agent_execution_failed", "agent_run_status_changed"]
        else:
            execution = asyncio.create_task(execute())
            try:
                async with asyncio.timeout(8):
                    await started.wait()
                    await service.cancel_run(request.run_id)
                    with pytest.raises(asyncio.CancelledError):
                        await execution
            finally:
                await _cancel_and_join(execution)
            assert len(received_cancellation) == len(propagated_cancellation) == 1
            assert propagated_cancellation[0] is received_cancellation[0]
            assert propagated_cancellation[0].__cause__ is None
            assert propagated_cancellation[0].__notes__ == ["agent_usage_persistence_failed"]
            target, code, result = Status.CANCELLED, None, None
            history = ["agent_run_status_changed"] * 2

        (write_error,) = failed_writes
        _assert_sequence_constraint(write_error)
        assert attempted == [*history, "agent_usage_recorded"]
        assert request.run_id not in registry
        runner.run.assert_awaited_once_with(request)
        model.ainvoke.assert_awaited_once_with([])
        assert await service.get_run_usage(request.run_id) is None
        await _assert_persisted(
            factory, request, workspace_id, target, CLAIM, (Status.RUNNING, target), result=result, failure=code,
        )

    database(check)


def test_agent_service_usage_survives_completion_transaction_rollback(database, monkeypatch):
    async def check(factory, request, workspace_id):
        attempted, failed_writes = _fail_event_write(monkeypatch, "agent_result_recorded")
        runner, research, finalizer = _training_usage_graph()
        run = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", run)
        registry = ActiveRunRegistry()
        service = AgentService(factory, runner, pricing=TRAINING_PRICING, active_runs=registry)

        with pytest.raises(IntegrityError) as exc:
            await service.execute_run(request.run_id)
        assert failed_writes == [exc.value]
        _assert_sequence_constraint(exc.value)
        assert attempted == ["agent_run_status_changed", "agent_result_recorded", "agent_usage_recorded"]
        assert request.run_id not in registry
        run.assert_awaited_once_with(request)
        assert research.ainvoke.await_count == 2
        finalizer.ainvoke.assert_awaited_once()
        assert await service.get_run_result(request.run_id) is None
        assert await service.get_run_failure(request.run_id) is None
        usage = await service.get_run_usage(request.run_id)
        assert usage == _training_usage(request)
        await _assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM, usage=usage)

    database(check)


@pytest.mark.parametrize("same_record", [True, False], ids=["replay", "conflict"])
def test_agent_service_serializes_concurrent_usage_writes(database, monkeypatch, same_record):
    async def check(factory, request, workspace_id):
        first_record = _training_usage(request)
        second_record = first_record if same_record else _training_usage(request, interrupted=True)
        staged, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pids = []
        original_append = AgentEventRepository.append
        original_lock = AgentRunRepository.get_for_update

        async def track_lock(repository, run_id):
            pids.append(await repository.session.scalar(text("SELECT pg_backend_pid()")))
            if len(pids) == 2:
                second_entered.set()
            return await original_lock(repository, run_id)

        async def hold_usage(repository, run_id, *, event_type, payload):
            event = await original_append(repository, run_id, event_type=event_type, payload=payload)
            await repository.session.flush()
            staged.set()
            await release.wait()
            return event

        async def write(record):
            async with factory() as session:
                await AgentRunService(session).record_usage(record)
                assert not session.in_transaction()

        monkeypatch.setattr(AgentEventRepository, "append", hold_usage)
        monkeypatch.setattr(AgentRunRepository, "get_for_update", track_lock)
        tasks = [asyncio.create_task(write(first_record))]
        try:
            async with asyncio.timeout(8):
                await staged.wait()
                tasks.append(asyncio.create_task(write(second_record)))
                await second_entered.wait()
                await _wait_for_run_blocker(factory, *pids, tasks[1])
                await _assert_persisted(factory, request, workspace_id, Status.CANCELLED)
                release.set()
                await tasks[0]
                if same_record:
                    await tasks[1]
                else:
                    with pytest.raises(AgentUsageContractError, match="^conflicting_agent_usage$"):
                        await tasks[1]
        finally:
            release.set()
            await _cancel_and_join(*tasks)

        await write(first_record)
        async with factory() as session:
            assert await AgentRunService(session).get_usage(request.run_id) == first_record
        await _assert_persisted(factory, request, workspace_id, Status.CANCELLED, usage=first_record)

    database(check, initial=Status.CANCELLED)


@pytest.mark.parametrize("case", ["missing", "historical", "unobserved", "queued"])
def test_agent_service_usage_read_and_write_boundaries(database, case):
    async def check(factory, request, workspace_id):
        result = _result()
        runner = SimpleNamespace(run=AsyncMock(return_value=result))
        service = AgentService(factory, runner)
        if case == "missing":
            missing_id = uuid4()
            with pytest.raises(AgentRunNotFoundError):
                await service.get_run_usage(missing_id)
            usage = _training_usage(request)
            missing_record = AgentUsageRecord(AgentUsageSnapshot(missing_id, usage.snapshot.calls), usage.pricing)
            async with factory() as session:
                with pytest.raises(AgentRunNotFoundError):
                    await AgentRunService(session).record_usage(missing_record)
                assert not session.in_transaction()
            target, edges, result = Status.QUEUED, (), None
        elif case == "historical":
            target, edges, result = Status.SUCCEEDED, (), None
        elif case == "unobserved":
            assert await service.execute_run(request.run_id) is result
            target, edges = Status.VERIFYING, (CLAIM, (Status.RUNNING, Status.VERIFYING))
        else:
            async with factory() as session:
                with pytest.raises(AgentUsageContractError, match="^usage_requires_claimed_run$"):
                    await AgentRunService(session).record_usage(_training_usage(request))
                assert not session.in_transaction()
            target, edges, result = Status.QUEUED, (), None

        assert await service.get_run_usage(request.run_id) is None
        if case == "unobserved":
            runner.run.assert_awaited_once_with(request)
        else:
            runner.run.assert_not_called()
        await _assert_persisted(factory, request, workspace_id, target, *edges, result=result)

    database(check, initial=Status.SUCCEEDED if case == "historical" else Status.QUEUED)


@pytest.mark.parametrize("case,reason", [
    ("corrupt", "invalid_stored_usage"), ("version", "unsupported_usage_version"), ("duplicate", "duplicate_agent_usage"),
])
def test_agent_service_usage_rejects_invalid_event_history(database, case, reason):
    async def check(factory, request, _workspace_id):
        usage = _training_usage(request)
        payload = encode_agent_usage(usage)
        if case == "corrupt":
            payload["calls"][0]["tokens"]["total_tokens"] = 1
        elif case == "version":
            payload["schema_version"] = 2
        copies = 2 if case == "duplicate" else 1
        async with factory.begin() as session:
            for _ in range(copies):
                await AgentEventRepository(session).append(request.run_id, event_type="agent_usage_recorded", payload=payload)

        runner = SimpleNamespace(run=AsyncMock())
        with pytest.raises(AgentUsageContractError, match=f"^{reason}$"):
            await AgentService(factory, runner).get_run_usage(request.run_id)
        runner.run.assert_not_called()
        async with factory() as session:
            with pytest.raises(AgentUsageContractError, match=f"^{reason}$"):
                await AgentRunService(session).record_usage(usage)
            assert not session.in_transaction()
            events = await AgentEventRepository(session).get_usage_events(request.run_id)
            assert [(event.sequence, event.payload) for event in events] == list(enumerate([payload] * copies, 1))

    database(check, initial=Status.FAILED)


def test_agent_service_concurrent_claim_loser_does_not_record_usage(database):
    async def check(factory, request, workspace_id):
        model = create_autospec(Runnable, instance=True)
        model.ainvoke.return_value = _usage_message("Synthetic response", TRAINING_TOKENS[0])
        started, release = asyncio.Event(), asyncio.Event()
        result = _result()

        async def run(_snapshot):
            await invoke_observed_model(model, [], stage=ModelCallStage.RESEARCH)
            started.set()
            await release.wait()
            return result

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        service = AgentService(factory, runner)
        winner = asyncio.create_task(service.execute_run(request.run_id))
        try:
            async with asyncio.timeout(8):
                await started.wait()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await service.execute_run(request.run_id)
                assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.RUNNING)
                assert await service.get_run_usage(request.run_id) is None
                release.set()
                assert await winner is result
        finally:
            release.set()
            await _cancel_and_join(winner)

        runner.run.assert_awaited_once_with(request)
        model.ainvoke.assert_awaited_once_with([])
        usage = _usage_record(request, _reported_usage_call(1, ModelCallStage.RESEARCH, TRAINING_TOKENS[0]))
        await _assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, CLAIM, (Status.RUNNING, Status.VERIFYING),
            result=result, usage=usage,
        )

    database(check)
