"""Opt-in tests on an already migrated, dedicated forge_task06_* database.

Run with both POSTGRES_DB and FORGE_TASK06_TEST_DB set to that database.
No DDL is performed; cleanup deletes only this test's UUID-scoped records.
"""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus as Status
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import encode_agent_result
from app.services.agent_runs import (
    AgentRunService,
    StaleAgentRunStatusError,
    VerificationEvidenceRequiredError,
)


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)


@pytest.fixture
def database():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail(
            "POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database",
            pytrace=False,
        )
    settings = Settings(postgres_db=name)

    @asynccontextmanager
    async def seeded(initial=Status.QUEUED):
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        workspace_id, task_id, run_id = uuid4(), uuid4(), uuid4()
        try:
            async with asyncio.timeout(30):
                async with factory() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic task06"))
                    await session.flush()
                    session.add(TaskModel(
                        id=task_id, workspace_id=workspace_id,
                        goal="synthetic transition verification", acceptance_criteria=["atomic"],
                    ))
                    await session.flush()
                    session.add(AgentRunModel(
                        id=run_id, task_id=task_id, attempt_number=1, status=initial.value,
                    ))
                    await session.commit()
                yield factory, run_id
        finally:
            try:
                async with asyncio.timeout(10):
                    async with factory.begin() as session:
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                        await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                        await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
            finally:
                await asyncio.wait_for(engine.dispose(), timeout=10)

    return seeded


async def snapshot(factory, run_id):
    """Read committed state in a new session, never the service's identity map."""
    async with factory() as session:
        status = await session.scalar(select(AgentRunModel.status).where(AgentRunModel.id == run_id))
        events = (await session.execute(
            select(AgentEventModel.id, AgentEventModel.sequence, AgentEventModel.event_type,
                   AgentEventModel.payload, AgentEventModel.created_at)
            .where(AgentEventModel.agent_run_id == run_id)
            .order_by(AgentEventModel.sequence)
        )).all()
        return status, events


def assert_events(events, transitions):
    assert len(events) == len(transitions)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    for event, (current, target) in zip(events, transitions, strict=True):
        assert event.event_type == "agent_run_status_changed"
        assert event.payload == {"from": current.value, "to": target.value}


async def transition(factory, run_id, current, target):
    async with factory() as session:
        await AgentRunService(session).transition_status(run_id, current, target)
        assert not session.in_transaction()


READY_RESULT = AgentFinalResult(
    summary="Synthetic analysis", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)


def test_successful_transition_is_visible_in_independent_session(database):
    async def check():
        async with database() as (factory, run_id):
            await transition(factory, run_id, Status.QUEUED, Status.RUNNING)
            status, events = await snapshot(factory, run_id)
            assert status == Status.RUNNING.value
            assert_events(events, [(Status.QUEUED, Status.RUNNING)])

    asyncio.run(check())


def test_stale_transition_preserves_committed_state(database):
    async def check():
        async with database() as (factory, run_id):
            await transition(factory, run_id, Status.QUEUED, Status.RUNNING)
            before = await snapshot(factory, run_id)
            with pytest.raises(StaleAgentRunStatusError) as exc:
                await transition(factory, run_id, Status.QUEUED, Status.RUNNING)
            assert exc.value.expected is Status.QUEUED
            assert exc.value.actual is Status.RUNNING
            assert await snapshot(factory, run_id) == before

    asyncio.run(check())


def test_sequential_transitions_have_ordered_events(database):
    async def check():
        async with database() as (factory, run_id):
            transitions = [
                (Status.QUEUED, Status.RUNNING),
                (Status.RUNNING, Status.VERIFYING),
                (Status.VERIFYING, Status.FAILED),
            ]
            await transition(factory, run_id, *transitions[0])
            async with factory() as session:
                await AgentRunService(session).complete_execution(run_id, READY_RESULT)
            await transition(factory, run_id, *transitions[2])
            status, events = await snapshot(factory, run_id)
            assert status == Status.FAILED.value
            assert [event.sequence for event in events] == [1, 2, 3, 4]
            assert events[1].event_type == "agent_result_recorded"
            assert events[1].payload == {"schema_version": 1, "result_status": "ready_for_verification"}
            for event, (current, target) in zip([events[0], *events[2:]], transitions, strict=True):
                assert event.event_type == "agent_run_status_changed"
                assert event.payload == {"from": current.value, "to": target.value}

    asyncio.run(check())


def test_event_constraint_failure_rolls_back_status_and_preserves_history(database, monkeypatch):
    async def check():
        async with database() as (factory, run_id):
            await transition(factory, run_id, Status.QUEUED, Status.RUNNING)
            before = await snapshot(factory, run_id)
            async with factory() as session:
                service = AgentRunService(session)
                original_add = service.events.add_status_changed

                async def duplicate_sequence(*args):
                    event = await original_add(*args)
                    # Prove the UPDATE reached PostgreSQL inside this transaction.
                    await session.flush()
                    assert await session.scalar(
                        select(AgentRunModel.status).where(AgentRunModel.id == run_id)
                    ) == Status.VERIFYING.value
                    assert await session.scalar(
                        select(AgentRunModel.final_result).where(AgentRunModel.id == run_id)
                    ) == encode_agent_result(READY_RESULT)
                    event.sequence = 1
                    return event

                monkeypatch.setattr(service.events, "add_status_changed", duplicate_sequence)
                with pytest.raises(IntegrityError) as exc:
                    await service.complete_execution(run_id, READY_RESULT)
                assert exc.value.orig.sqlstate == "23505"
                assert not session.in_transaction()
            assert await snapshot(factory, run_id) == before
            async with factory() as session:
                assert await session.scalar(
                    select(AgentRunModel.final_result.is_(None)).where(AgentRunModel.id == run_id)
                ) is True

    asyncio.run(check())


@pytest.mark.parametrize("winner,loser", [
    (Status.RUNNING, Status.CANCELLED),
    (Status.CANCELLED, Status.RUNNING),
])
def test_concurrent_stale_request_waits_for_row_lock(database, monkeypatch, winner, loser):
    async def check():
        async with database() as (factory, run_id):
            async with factory() as first, factory() as second, factory() as observer:
                first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
                second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
                assert first_pid != second_pid
                service = AgentRunService(first)
                locked, release = asyncio.Event(), asyncio.Event()
                original_get = service.runs.get_for_update

                async def hold_lock(key):
                    run = await original_get(key)
                    locked.set()
                    await release.wait()
                    return run

                monkeypatch.setattr(service.runs, "get_for_update", hold_lock)
                tasks = []
                try:
                    async with asyncio.timeout(10):
                        tasks.append(asyncio.create_task(service.transition_status(run_id, Status.QUEUED, winner)))
                        await locked.wait()
                        tasks.append(asyncio.create_task(
                            AgentRunService(second).transition_status(run_id, Status.QUEUED, loser)
                        ))
                        # PostgreSQL itself must confirm B is blocked by A before A commits.
                        while True:
                            blockers = await observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                            )
                            if first_pid in blockers:
                                break
                            assert not tasks[1].done(), "Competing transition did not wait for the row lock"
                            await asyncio.sleep(0.01)
                        assert not tasks[0].done() and not tasks[1].done()
                        release.set()
                        await tasks[0]
                        with pytest.raises(StaleAgentRunStatusError) as exc:
                            await tasks[1]
                        assert exc.value.expected is Status.QUEUED
                        assert exc.value.actual is winner
                        assert not first.in_transaction() and not second.in_transaction()
                finally:
                    release.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            status, events = await snapshot(factory, run_id)
            assert status == winner.value
            assert_events(events, [(Status.QUEUED, winner)])

    asyncio.run(check())


def test_succeeded_requires_evidence(database):
    async def check():
        async with database(Status.VERIFYING) as (factory, run_id):
            with pytest.raises(VerificationEvidenceRequiredError):
                await transition(factory, run_id, Status.VERIFYING, Status.SUCCEEDED)
            status, events = await snapshot(factory, run_id)
            assert status == Status.VERIFYING.value
            assert events == []

    asyncio.run(check())


def test_repositories_leave_commit_and_rollback_to_caller(database):
    async def check():
        async with database() as (factory, run_id):
            before = await snapshot(factory, run_id)
            async with factory() as session:
                transaction = await session.begin()
                run = await AgentRunRepository(session).get_for_update(run_id)
                assert run is not None
                assert session.get_transaction() is transaction
                run.status = Status.RUNNING.value
                await AgentEventRepository(session).add_status_changed(run_id, Status.QUEUED, Status.RUNNING)
                await session.flush()
                assert session.get_transaction() is transaction
                assert await snapshot(factory, run_id) == before
                await session.rollback()
            assert await snapshot(factory, run_id) == before

    asyncio.run(check())
