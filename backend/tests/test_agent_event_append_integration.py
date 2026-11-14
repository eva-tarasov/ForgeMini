"""Opt-in AgentEvent tests on an already migrated forge_task06_* database."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus as Status
from app.repositories.agent_events import AgentEventParentNotFoundError, AgentEventRepository
from app.services.agent_runs import AgentRunService


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)

SYNTHETIC_TYPE = "synthetic_observation"
STATUS_EVENT = ("agent_run_status_changed", {"from": "queued", "to": "running"})


@pytest.fixture
def database():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    settings = Settings(postgres_db=name)

    @asynccontextmanager
    async def seeded(*, run_count=1):
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        workspace_id, task_id = uuid4(), uuid4()
        run_ids = tuple(uuid4() for _ in range(run_count))
        seeded = False
        try:
            async with asyncio.timeout(30):
                async with factory() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic event append"))
                    await session.flush()
                    session.add(TaskModel(
                        id=task_id, workspace_id=workspace_id, goal="synthetic event history",
                        acceptance_criteria=["ordered per run"],
                    ))
                    await session.flush()
                    session.add_all([
                        AgentRunModel(id=run_id, task_id=task_id, attempt_number=number, status=Status.QUEUED.value)
                        for number, run_id in enumerate(run_ids, 1)
                    ])
                    await session.flush()
                    seeded = True
                    await session.commit()
                yield factory, task_id, *run_ids
        finally:
            try:
                if seeded:
                    async with asyncio.timeout(10), factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        ids = select(AgentRunModel.id).where(AgentRunModel.task_id == task_id)
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id.in_(ids)))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.task_id == task_id))
                        await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                        await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
            finally:
                await asyncio.wait_for(engine.dispose(), timeout=10)

    def run(scenario, *, run_count=1):
        async def check():
            async with seeded(run_count=run_count) as data:
                await scenario(*data)

        asyncio.run(check())

    return run


async def snapshot(factory, run_id):
    async with factory() as session:
        status = await session.scalar(select(AgentRunModel.status).where(AgentRunModel.id == run_id))
        result = await session.execute(
            select(AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload)
            .where(AgentEventModel.agent_run_id == run_id)
            .order_by(AgentEventModel.sequence)
        )
        return status, list(result.tuples())


async def append(session, run_id, label):
    return await AgentEventRepository(session).append(run_id, event_type=SYNTHETIC_TYPE, payload={"label": label})


@asynccontextmanager
async def blocked_writer(factory, first_pid, second, write):
    second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
    assert first_pid != second_pid
    entered = asyncio.Event()

    async def compete():
        entered.set()
        return await write()

    task = asyncio.create_task(compete())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            async with factory() as observer:
                while True:
                    blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid})
                    if first_pid in blockers:
                        break
                    assert not task.done(), "Second writer finished before the expected PostgreSQL row lock wait"
                    await asyncio.sleep(0.01)  # Pace polling; PostgreSQL confirms the lock wait.
            assert not task.done()
            yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


def test_agent_event_append_shares_sequence_with_status_wrapper(database):
    async def check(factory, _task_id, run_id):
        payload = {"content": ["synthetic", {"observed": True}]}
        event_type = "x" * 100
        async with factory() as session:
            repository = AgentEventRepository(session)
            first = await repository.append(run_id, event_type=event_type, payload=payload)
            second = await repository.add_status_changed(run_id, Status.QUEUED, Status.RUNNING)
            third = await append(session, run_id, "third")
            assert isinstance(first, AgentEventModel)
            assert (first.agent_run_id, first.event_type, first.payload) == (run_id, event_type, payload)
            assert isinstance(second, AgentEventModel)
            assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
            assert third in session.new
            assert await snapshot(factory, run_id) == ("queued", [])
            await session.commit()

        assert await snapshot(factory, run_id) == ("queued", [
            (1, event_type, payload), (2, *STATUS_EVENT), (3, SYNTHETIC_TYPE, {"label": "third"}),
        ])

    database(check)


def test_agent_event_append_with_autoflush_disabled(database):
    async def check(factory, _task_id, run_id):
        async with factory(autoflush=False) as session:
            transaction = await session.begin()
            first = await append(session, run_id, "first")
            assert first in session.new
            second = await append(session, run_id, "second")
            assert [first.sequence, second.sequence] == [1, 2]
            assert first not in session.new and second in session.new
            assert session.get_transaction() is transaction and transaction.is_active
            assert await snapshot(factory, run_id) == ("queued", [])
            await transaction.commit()

        assert await snapshot(factory, run_id) == ("queued", [
            (1, SYNTHETIC_TYPE, {"label": "first"}), (2, SYNTHETIC_TYPE, {"label": "second"}),
        ])

    database(check)


async def competing_appends(factory, run_id, *, rollback_first):
    async with factory() as first, factory() as second:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        first_event = await append(first, run_id, "first")
        assert first_event.sequence == 1
        await first.flush()
        assert await snapshot(factory, run_id) == ("queued", [])

        async with blocked_writer(factory, first_pid, second, lambda: append(second, run_id, "second")) as pending:
            if rollback_first:
                await first.rollback()
            else:
                await first.commit()
            second_event = await pending
            assert second_event.sequence == (1 if rollback_first else 2)
            await second.commit()

    labels = ["second"] if rollback_first else ["first", "second"]
    assert await snapshot(factory, run_id) == ("queued", [
        (number, SYNTHETIC_TYPE, {"label": label}) for number, label in enumerate(labels, 1)
    ])


def test_agent_event_append_serializes_concurrent_writers(database):
    async def check(factory, _task_id, run_id):
        await competing_appends(factory, run_id, rollback_first=False)

    database(check)


def test_agent_event_append_rollback(database):
    async def check(factory, _task_id, run_id):
        await competing_appends(factory, run_id, rollback_first=True)

    database(check)


@pytest.mark.parametrize("first_writer", ["generic", "status"])
def test_agent_event_append_serializes_with_status_transition(database, monkeypatch, first_writer):
    async def check(factory, _task_id, run_id):
        async with factory() as first, factory() as second:
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            if first_writer == "generic":
                event = await append(first, run_id, "generic")
                await first.flush()
                service = AgentRunService(second)
                async with blocked_writer(
                    factory, first_pid, second,
                    lambda: service.transition_status(run_id, Status.QUEUED, Status.RUNNING),
                ) as pending:
                    await first.commit()
                    await pending
                assert event.sequence == 1
                expected = [(1, SYNTHETIC_TYPE, {"label": "generic"}), (2, *STATUS_EVENT)]
            else:
                service = AgentRunService(first)
                held, release = asyncio.Event(), asyncio.Event()
                add_status_changed = service.events.add_status_changed

                async def hold_status_event(*args):
                    event = await add_status_changed(*args)
                    await first.flush()
                    held.set()
                    await release.wait()
                    return event

                monkeypatch.setattr(service.events, "add_status_changed", hold_status_event)
                status_task = asyncio.create_task(service.transition_status(run_id, Status.QUEUED, Status.RUNNING))
                try:
                    await asyncio.wait_for(held.wait(), timeout=5)
                    async with blocked_writer(
                        factory, first_pid, second, lambda: append(second, run_id, "generic")
                    ) as pending:
                        assert await snapshot(factory, run_id) == ("queued", [])
                        release.set()
                        await status_task
                        event = await pending
                        assert event.sequence == 2
                        await second.commit()
                finally:
                    release.set()
                    if not status_task.done():
                        status_task.cancel()
                    await asyncio.wait_for(asyncio.gather(status_task, return_exceptions=True), timeout=5)
                expected = [(1, *STATUS_EVENT), (2, SYNTHETIC_TYPE, {"label": "generic"})]

        assert await snapshot(factory, run_id) == ("running", expected)

    database(check)


def test_agent_event_append_scopes_lock_and_sequence_to_run(database):
    async def check(factory, _task_id, first_id, second_id):
        async with factory() as first, factory() as second:
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid != second_pid
            first_event = await append(first, first_id, "first run")
            await first.flush()
            async with asyncio.timeout(5):
                second_event = await append(second, second_id, "second run")
                await second.commit()

            assert first_event.sequence == second_event.sequence == 1
            assert await snapshot(factory, first_id) == ("queued", [])
            assert await snapshot(factory, second_id) == ("queued", [(1, SYNTHETIC_TYPE, {"label": "second run"})])
            await first.commit()

        assert await snapshot(factory, first_id) == ("queued", [(1, SYNTHETIC_TYPE, {"label": "first run"})])

    database(check, run_count=2)


def test_agent_event_append_rejects_missing_parent(database):
    async def check(factory, _task_id, _run_id):
        missing_id = uuid4()
        async with factory() as session:
            transaction = await session.begin()
            with pytest.raises(AgentEventParentNotFoundError, match="^agent_run_not_found$"):
                await append(session, missing_id, "missing")
            assert session.get_transaction() is transaction and transaction.is_active
            assert not session.new
            await transaction.rollback()
        assert await snapshot(factory, missing_id) == (None, [])

    database(check)


def test_agent_event_append_requires_parent_to_be_flushed(database):
    async def check(factory, task_id, _run_id):
        run_id = uuid4()
        async with factory.begin() as session:
            run = AgentRunModel(id=run_id, task_id=task_id, attempt_number=2, status=Status.QUEUED.value)
            session.add(run)
            with pytest.raises(AgentEventParentNotFoundError, match="^agent_run_not_found$"):
                await append(session, run_id, "too early")
            assert list(session.new) == [run]
            await session.flush()
            event = await append(session, run_id, "flushed parent")
            assert event.sequence == 1

        assert await snapshot(factory, run_id) == ("queued", [(1, SYNTHETIC_TYPE, {"label": "flushed parent"})])

    database(check)


@pytest.mark.parametrize("event_type,payload,error,reason", [
    ("", {}, ValueError, "invalid_event_type"),
    (" \t\n", {}, ValueError, "invalid_event_type"),
    ("x" * 101, {}, ValueError, "invalid_event_type"),
    (1, {}, ValueError, "invalid_event_type"),
    (SYNTHETIC_TYPE, [], TypeError, "invalid_event_payload"),
])
def test_agent_event_append_rejects_invalid_input(database, event_type, payload, error, reason):
    async def check(factory, _task_id, run_id):
        async with factory.begin() as session:
            with pytest.raises(error, match=f"^{reason}$"):
                await AgentEventRepository(session).append(run_id, event_type=event_type, payload=payload)
            assert session.in_transaction()
            assert not session.new
        assert await snapshot(factory, run_id) == ("queued", [])

    database(check)


def test_agent_event_append_leaves_rollback_to_caller(database):
    async def check(factory, _task_id, run_id):
        async with factory() as session:
            transaction = await session.begin()
            await append(session, run_id, "first")
            await append(session, run_id, "second")
            await session.flush()
            assert session.get_transaction() is transaction and transaction.is_active
            assert await snapshot(factory, run_id) == ("queued", [])
            await transaction.rollback()

        assert await snapshot(factory, run_id) == ("queued", [])

    database(check)
