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
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_result_codec import encode_agent_result
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunNotFoundError, AgentRunService, StaleAgentRunStatusError
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


async def _assert_persisted(factory, request, workspace_id, status, *edges, result=None):
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
        await _assert_persisted(factory, request, workspace_id, Status.FAILED, CLAIM, (Status.RUNNING, Status.FAILED))

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
