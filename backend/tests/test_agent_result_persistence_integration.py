"""Verify result persistence in a dedicated, already migrated PostgreSQL database."""

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
from sqlalchemy import JSON, delete, func, select, text, update
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
from app.services.agent_cost import AgentPricing, CostStatus
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_result_codec import AgentResultContractError, encode_agent_result
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import (
    AgentRunNotFoundError,
    AgentRunResultAlreadyRecordedError,
    AgentRunResultRequiredError,
    AgentRunService,
    StaleAgentRunStatusError,
    VerificationEvidenceRequiredError,
)
from app.services.agent_service import AgentService
from app.services.agent_usage import AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, UsageStatus
from app.services.agent_usage_codec import AgentUsageRecord, encode_agent_usage


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)

GOAL = "Проверить предварительный анализ"
CRITERIA = ("Сохранить порядок", "Прочитать fixture")
FIXTURE_TEXT = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)
CLAIM = ("agent_run_status_changed", {"from": "queued", "to": "running"})


@pytest.fixture(autouse=True)
def disable_model_tracing(monkeypatch):
    for key, value in TRACING_OFF.items():
        monkeypatch.setenv(key, value)
    with tracing_context(enabled=False, parent=False):
        yield


@pytest.fixture
def settings():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    return Settings(postgres_db=name)


@pytest.fixture
def database(settings):
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
                    assert await session.scalar(text("SELECT current_database()")) == settings.postgres_db
                    assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                    assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "1d972cc34fbe"
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic result persistence"))
                    await session.flush()
                    session.add(TaskModel(
                        id=task_id, workspace_id=workspace_id, goal=GOAL, acceptance_criteria=list(CRITERIA),
                        status="queued",
                    ))
                    await session.flush()
                    session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status=initial.value))
                    await session.flush()
                    seeded = True
                    await session.commit()
                yield factory, request, workspace_id
        finally:
            try:
                if seeded:
                    async with asyncio.timeout(10), factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == settings.postgres_db
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


def make_result(blocker=None):
    return AgentFinalResult(
        summary="Синтетический анализ",
        status="ready_for_verification" if blocker is None else "blocked",
        relevant_components=("task_service", "agent_run_service"),
        assumptions=("Второе", "Первое"),
        observations=(FixtureObservation(component="task_service", tool_call_id="fixture-1", content=FIXTURE_TEXT),),
        blocked_reason=blocker,
    )


def status_event(target):
    return ("agent_run_status_changed", {"from": "running", "to": target.value})


def result_event(result):
    return ("agent_result_recorded", {"schema_version": 1, "result_status": result.status})


async def assert_persisted(factory, request, workspace_id, status, *expected, document=None):
    async with factory() as session:
        run = await session.get(AgentRunModel, request.run_id)
        task = await session.get(TaskModel, request.task_id)
        column = AgentRunModel.final_result
        storage = (await session.execute(
            select(column.is_(None), func.jsonb_typeof(column)).where(AgentRunModel.id == run.id)
        )).one()
        events = (await session.execute(
            select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload)
            .where(AgentEventModel.agent_run_id == run.id).order_by(AgentEventModel.sequence)
        )).all()
        assert (run.task_id, run.status, run.attempt_number, run.final_result) == (
            request.task_id, status.value, 1, document,
        )
        assert storage == ((True, None) if document is None else (False, "object"))
        assert (task.workspace_id, task.goal, task.acceptance_criteria, task.status, task.idempotency_key) == (
            workspace_id, GOAL, list(CRITERIA), "queued", None,
        )
        assert events == [(number, *event) for number, event in enumerate(expected, 1)]


@pytest.mark.parametrize("blocker,target", [(None, Status.VERIFYING), ("Нужны данные", Status.FAILED)])
def test_agent_result_persists_exact_envelope_and_events(database, blocker, target):
    async def check(factory, request, workspace_id):
        result = make_result(blocker)
        runner = SimpleNamespace(run=AsyncMock(return_value=result))
        service = AgentService(factory, runner)
        assert await service.get_run_result(request.run_id) is None
        assert await service.execute_run(request.run_id) is result
        assert await service.get_run_result(request.run_id) == result
        runner.run.assert_awaited_once_with(request)
        await assert_persisted(
            factory, request, workspace_id, target, CLAIM, result_event(result), status_event(target),
            document={"schema_version": 1, "result": result.model_dump(mode="json")},
        )

    database(check)


def test_agent_result_persists_across_engines(database, settings, monkeypatch):
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
            AIMessage(content="Fixture прочитан."),
        ]
        finalizer = create_autospec(Runnable, instance=True)
        expected = make_result()
        finalizer.ainvoke.return_value = {
            "raw": AIMessage(content=""),
            "parsed": TaskAnalysis(
                summary=expected.summary, relevant_components=list(expected.relevant_components),
                assumptions=list(expected.assumptions), blocked_reason=None,
            ),
            "parsing_error": None,
        }

        execution_engine = create_engine(settings)
        try:
            runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))
            service = AgentService(create_session_factory(execution_engine), runner)
            original_result = await service.execute_run(request.run_id)
        finally:
            await asyncio.wait_for(execution_engine.dispose(), timeout=10)
        assert original_result == expected
        assert research.ainvoke.await_count == 2
        execution.assert_awaited_once_with(component="task_service")
        finalizer.ainvoke.assert_awaited_once()

        reader_engine = create_engine(settings)
        try:
            reader_factory = create_session_factory(reader_engine)
            unused_runner = SimpleNamespace(run=AsyncMock())
            reader = AgentService(reader_factory, unused_runner)
            loaded = await reader.get_run_result(request.run_id)
            assert loaded == original_result and type(loaded) is type(original_result) is AgentFinalResult
            assert type(loaded.observations) is tuple and type(loaded.observations[0]) is FixtureObservation
            assert type(loaded.relevant_components) is tuple and type(loaded.assumptions) is tuple
            usage = AgentUsageRecord(AgentUsageSnapshot(request.run_id, tuple(
                ModelCallUsage(number, stage, ModelCallOutcome.RETURNED, UsageStatus.MISSING, None)
                for number, stage in enumerate(
                    (ModelCallStage.RESEARCH, ModelCallStage.RESEARCH, ModelCallStage.FINALIZATION), 1,
                )
            )), AgentPricing())
            stored = await reader.get_run_usage(request.run_id)
            assert stored == usage and not stored.snapshot.usage_complete and not stored.cost_estimate.cost_complete
            assert [call.status for call in stored.cost_estimate.calls] == [CostStatus.USAGE_UNKNOWN] * 3
            unused_runner.run.assert_not_called()
            await assert_persisted(
                reader_factory, request, workspace_id, Status.VERIFYING, CLAIM, result_event(expected),
                status_event(Status.VERIFYING), ("agent_usage_recorded", encode_agent_usage(usage)),
                document=encode_agent_result(expected),
            )
        finally:
            await asyncio.wait_for(reader_engine.dispose(), timeout=10)

    database(check)


@pytest.mark.parametrize("failure_type", ["agent_result_recorded", "agent_run_status_changed"])
def test_agent_result_completion_is_atomic(database, monkeypatch, failure_type):
    async def check(factory, request, workspace_id):
        original = AgentEventRepository.append
        attempted = []
        persistence_errors = []
        result = make_result()

        async def append_then_fail(repository, run_id, *, event_type, payload):
            attempted.append((event_type, payload))
            event = await original(repository, run_id, event_type=event_type, payload=payload)
            if len(attempted) == 1 or event_type != failure_type:
                return event
            session = repository.session
            await session.flush()
            status, document = (await session.execute(
                select(AgentRunModel.status, AgentRunModel.final_result).where(AgentRunModel.id == run_id)
            )).one()
            assert status == ("running" if failure_type == "agent_result_recorded" else "verifying")
            assert document == encode_agent_result(result)
            rows = (await session.execute(
                select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload)
                .where(AgentEventModel.agent_run_id == run_id).order_by(AgentEventModel.sequence)
            )).all()
            assert rows == [(number, *item) for number, item in enumerate(attempted, 1)]
            event.sequence = 0
            try:
                await session.flush()
            except IntegrityError as error:
                persistence_errors.append(error)
                raise

        monkeypatch.setattr(AgentEventRepository, "append", append_then_fail)
        runner = SimpleNamespace(run=AsyncMock(return_value=result))
        with pytest.raises(IntegrityError) as exc:
            await AgentService(factory, runner).execute_run(request.run_id)
        assert persistence_errors == [exc.value]
        assert exc.value.orig.sqlstate == "23514"
        assert exc.value.orig.__cause__.constraint_name == "ck_agent_events_sequence_positive"
        runner.run.assert_awaited_once_with(request)
        expected = [CLAIM, result_event(result)]
        if failure_type == "agent_run_status_changed":
            expected.append(status_event(Status.VERIFYING))
        assert attempted == expected
        await assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)

    database(check)


async def cancel_and_join(task):
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


@pytest.mark.parametrize("blocker", [None, "Нужны данные"])
def test_agent_result_rejects_late_completion(database, blocker):
    async def check(factory, request, workspace_id):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(snapshot):
            assert snapshot == request
            started.set()
            await release.wait()
            return make_result(blocker)

        runner = SimpleNamespace(run=AsyncMock(side_effect=run))
        execution = asyncio.create_task(AgentService(factory, runner).execute_run(request.run_id))
        cancellation = status_event(Status.CANCELLED)
        try:
            async with asyncio.timeout(10):
                await started.wait()
                await assert_persisted(factory, request, workspace_id, Status.RUNNING, CLAIM)
                async with factory() as session:
                    await AgentRunService(session).transition_status(request.run_id, Status.RUNNING, Status.CANCELLED)
                await assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancellation)
                assert not execution.done()
                release.set()
                with pytest.raises(StaleAgentRunStatusError) as exc:
                    await execution
                assert (exc.value.expected, exc.value.actual) == (Status.RUNNING, Status.CANCELLED)
        finally:
            release.set()
            await cancel_and_join(execution)
        runner.run.assert_awaited_once_with(request)
        await assert_persisted(factory, request, workspace_id, Status.CANCELLED, CLAIM, cancellation)

    database(check)


@pytest.mark.parametrize("case", ["missing", "corrupt", "unsupported"])
def test_agent_result_read_rejects_missing_or_invalid_document(database, case):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        service = AgentService(factory, runner)
        if case == "missing":
            with pytest.raises(AgentRunNotFoundError):
                await service.get_run_result(uuid4())
            await assert_persisted(factory, request, workspace_id, Status.QUEUED)
        else:
            document = {"schema_version": 1, "result": {}} if case == "corrupt" else {
                "schema_version": 2, "result": make_result().model_dump(mode="json"),
            }
            async with factory.begin() as session:
                await session.execute(update(AgentRunModel).where(AgentRunModel.id == request.run_id).values(
                    final_result=document
                ))
            reason = "invalid_stored_result" if case == "corrupt" else "unsupported_result_version"
            with pytest.raises(AgentResultContractError, match=f"^{reason}$"):
                await service.get_run_result(request.run_id)
            await assert_persisted(factory, request, workspace_id, Status.QUEUED, document=document)
        runner.run.assert_not_called()

    database(check)


def test_agent_result_preserves_historical_verifying_and_succeeded_guard(database):
    async def check(factory, request, workspace_id):
        runner = SimpleNamespace(run=AsyncMock())
        assert await AgentService(factory, runner).get_run_result(request.run_id) is None
        runner.run.assert_not_called()
        async with factory() as session:
            with pytest.raises(VerificationEvidenceRequiredError):
                await AgentRunService(session).transition_status(request.run_id, Status.VERIFYING, Status.SUCCEEDED)
            assert not session.in_transaction()
        await assert_persisted(factory, request, workspace_id, Status.VERIFYING)

    database(check, initial=Status.VERIFYING)


@pytest.mark.parametrize("stored", ["none", "blocked", "corrupt", "unsupported", "ready"])
def test_agent_result_guards_direct_verifying_transition(database, stored):
    async def check(factory, request, workspace_id):
        document = None
        if stored in ("ready", "blocked"):
            document = encode_agent_result(make_result(None if stored == "ready" else "Нужны данные"))
        if stored in ("corrupt", "unsupported"):
            document = {"schema_version": 1 if stored == "corrupt" else 2, "result": {}}
        if document is not None:
            async with factory.begin() as session:
                await session.execute(update(AgentRunModel).where(AgentRunModel.id == request.run_id).values(
                    final_result=document
                ))
        errors = {
            "none": (AgentRunResultRequiredError, "final_result_required"),
            "blocked": (AgentRunResultRequiredError, "result_not_ready_for_verification"),
            "corrupt": (AgentResultContractError, "invalid_stored_result"),
            "unsupported": (AgentResultContractError, "unsupported_result_version"),
        }
        async with factory() as session:
            service = AgentRunService(session)
            if stored == "ready":
                await service.transition_status(request.run_id, Status.RUNNING, Status.VERIFYING)
            else:
                error, reason = errors[stored]
                with pytest.raises(error, match=f"^{reason}$"):
                    await service.transition_status(request.run_id, Status.RUNNING, Status.VERIFYING)
            assert not session.in_transaction()
        if stored == "ready":
            await assert_persisted(
                factory, request, workspace_id, Status.VERIFYING, status_event(Status.VERIFYING), document=document,
            )
        else:
            await assert_persisted(factory, request, workspace_id, Status.RUNNING, document=document)

    database(check, initial=Status.RUNNING)


def test_agent_result_refuses_to_replace_a_completed_result(database):
    async def check(factory, request, workspace_id):
        ready = make_result()
        async with factory() as session:
            service = AgentRunService(session)
            await service.complete_execution(request.run_id, ready)
            with pytest.raises(StaleAgentRunStatusError) as exc:
                await service.complete_execution(request.run_id, make_result("Нужны данные"))
            assert (exc.value.expected, exc.value.actual) == (Status.RUNNING, Status.VERIFYING)
        await assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, result_event(ready), status_event(Status.VERIFYING),
            document=encode_agent_result(ready),
        )

    database(check, initial=Status.RUNNING)


def test_agent_result_rejects_already_recorded_result_on_running_run(database):
    async def check(factory, request, workspace_id):
        document = encode_agent_result(make_result())
        async with factory.begin() as session:
            await session.execute(update(AgentRunModel).where(AgentRunModel.id == request.run_id).values(
                final_result=document
            ))
        async with factory() as session:
            with pytest.raises(AgentRunResultAlreadyRecordedError, match="^result_already_recorded$"):
                await AgentRunService(session).complete_execution(request.run_id, make_result("Нужны данные"))
            assert not session.in_transaction()
        await assert_persisted(factory, request, workspace_id, Status.RUNNING, document=document)

    database(check, initial=Status.RUNNING)


def test_agent_result_serializes_concurrent_completions(database, monkeypatch):
    async def check(factory, request, workspace_id):
        winner, loser = make_result(), make_result("Нужны данные")
        staged, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async with factory() as first, factory() as second:
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid != second_pid
            cached_run = await second.get(AgentRunModel, request.run_id)
            assert cached_run.status == "running" and cached_run.final_result is None
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
                await AgentRunService(second).complete_execution(request.run_id, loser)

            monkeypatch.setattr(first_service.events, "add_status_changed", stage_and_wait)
            tasks = [asyncio.create_task(first_service.complete_execution(request.run_id, winner))]
            try:
                async with asyncio.timeout(10):
                    await staged.wait()
                    tasks.append(asyncio.create_task(compete()))
                    await entered.wait()
                    async with factory() as observer:
                        while first_pid not in await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                        ):
                            assert not tasks[1].done(), "Second completion did not wait on the run lock"
                            await asyncio.sleep(0.01)
                    assert not tasks[1].done()
                    await assert_persisted(factory, request, workspace_id, Status.RUNNING)
                    release.set()
                    await tasks[0]
                    with pytest.raises(StaleAgentRunStatusError) as exc:
                        await tasks[1]
                    assert (exc.value.expected, exc.value.actual) == (Status.RUNNING, Status.VERIFYING)
            finally:
                release.set()
                await asyncio.gather(*(cancel_and_join(task) for task in tasks))
        await assert_persisted(
            factory, request, workspace_id, Status.VERIFYING, result_event(winner), status_event(Status.VERIFYING),
            document=encode_agent_result(winner),
        )

    database(check, initial=Status.RUNNING)


@pytest.mark.parametrize("value", [JSON.NULL, "synthetic", [], 42], ids=["json-null", "string", "array", "number"])
def test_agent_result_database_rejects_non_object_jsonb(database, value):
    async def check(factory, request, workspace_id):
        async with factory() as session:
            with pytest.raises(IntegrityError) as exc:
                await session.execute(update(AgentRunModel).where(AgentRunModel.id == request.run_id).values(
                    final_result=value
                ))
            assert exc.value.orig.sqlstate == "23514"
            assert exc.value.orig.__cause__.constraint_name == "ck_agent_runs_final_result_object"
            await session.rollback()
        await assert_persisted(factory, request, workspace_id, Status.QUEUED)

    database(check)


@pytest.mark.parametrize("failure", ["runner-error", "unsupported", "corrupt-ready"])
def test_agent_result_execution_failures_leave_sql_null(database, failure):
    async def check(factory, request, workspace_id):
        if failure == "runner-error":
            error = RuntimeError("synthetic execution error")
            run = AsyncMock(side_effect=error)
        else:
            invalid = make_result().model_copy(update={
                "status": "unsupported",
            } if failure == "unsupported" else {"summary": " "})
            run = AsyncMock(return_value=invalid)
        with pytest.raises(RuntimeError if failure == "runner-error" else AgentRunnerError) as exc:
            await AgentService(factory, SimpleNamespace(run=run)).execute_run(request.run_id)
        if failure == "runner-error":
            assert exc.value is error
        else:
            assert exc.value.reason == "invalid_runner_result"
        run.assert_awaited_once_with(request)
        code = "execution_failed" if failure == "runner-error" else "invalid_runner_result"
        failure_event = ("agent_execution_failed", {"schema_version": 1, "code": code})
        await assert_persisted(
            factory, request, workspace_id, Status.FAILED, CLAIM, failure_event, status_event(Status.FAILED)
        )

    database(check)
