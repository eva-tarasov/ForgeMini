"""Opt-in Task transitions on a dedicated, already migrated Task 06 database."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.core.settings import Settings
from app.db.models import TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.task import TaskStatus as Status
from app.domain.task_transitions import InvalidTaskTransition
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.tasks import (
    StaleTaskStatusError,
    TaskService,
    TaskVerificationEvidenceRequiredError,
)


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)


@pytest.fixture
def database():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    settings = Settings(postgres_db=name)

    @asynccontextmanager
    async def seeded(initial=Status.QUEUED):
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        workspace_id, task_id = uuid4(), uuid4()
        created = False
        try:
            async with asyncio.timeout(30):
                async with factory() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic task06 transition"))
                    await session.flush()
                    session.add(TaskModel(
                        id=task_id, workspace_id=workspace_id, goal="synthetic task transition",
                        acceptance_criteria=["persisted", "isolated"], status=initial.value,
                    ))
                    await session.flush()
                    # Only clean up IDs after both inserts succeeded; covers uncertain commit outcomes.
                    created = True
                    await session.commit()
                yield factory, task_id, workspace_id
        finally:
            try:
                if created:
                    async with asyncio.timeout(10):
                        async with factory.begin() as session:
                            await session.execute(delete(TaskModel).where(
                                TaskModel.id == task_id, TaskModel.workspace_id == workspace_id
                            ))
                            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
            finally:
                await asyncio.wait_for(engine.dispose(), timeout=10)

    return seeded


def service_for(session):
    return TaskService(session, TaskRepository(session), WorkspaceRepository(session))


async def transition(factory, task_id, current, target):
    async with factory() as session:
        await service_for(session).transition_status(task_id, current, target)
        assert not session.in_transaction()


async def snapshot(factory, task_id):
    async with factory() as session:
        return (await session.execute(
            select(TaskModel.id, TaskModel.workspace_id, TaskModel.goal,
                   TaskModel.acceptance_criteria, TaskModel.status).where(TaskModel.id == task_id)
        )).one()


def test_transition_persists_and_repository_maps_status(database):
    async def check():
        async with database() as (factory, task_id, workspace_id):
            before = await snapshot(factory, task_id)
            await transition(factory, task_id, Status.QUEUED, Status.RUNNING)
            async with factory() as session:
                task = await TaskRepository(session).get(task_id)
                assert task is not None and task.status is Status.RUNNING
                assert (task.id, task.workspace_id, task.goal, task.acceptance_criteria) == (
                    task_id, workspace_id, "synthetic task transition", ("persisted", "isolated"),
                )
            after = await snapshot(factory, task_id)
            assert after[:-1] == before[:-1]
            assert before.status == "queued" and after.status == "running"

    asyncio.run(check())


def test_stale_transition_preserves_status(database):
    async def check():
        async with database() as (factory, task_id, _):
            await transition(factory, task_id, Status.QUEUED, Status.RUNNING)
            before = await snapshot(factory, task_id)
            with pytest.raises(StaleTaskStatusError) as exc:
                await transition(factory, task_id, Status.QUEUED, Status.CANCELLED)
            assert exc.value.expected is Status.QUEUED and exc.value.actual is Status.RUNNING
            assert before.status == "running"
            assert await snapshot(factory, task_id) == before

    asyncio.run(check())


@pytest.mark.parametrize("initial,target,error", [
    (Status.FAILED, Status.RUNNING, InvalidTaskTransition),
    (Status.CANCELLED, Status.RUNNING, InvalidTaskTransition),
    (Status.VERIFYING, Status.SUCCEEDED, TaskVerificationEvidenceRequiredError),
])
def test_terminal_and_evidence_rejections_preserve_state(database, initial, target, error):
    async def check():
        async with database(initial) as (factory, task_id, _):
            before = await snapshot(factory, task_id)
            with pytest.raises(error):
                await transition(factory, task_id, initial, target)
            assert before.status == initial.value
            assert await snapshot(factory, task_id) == before

    asyncio.run(check())


def test_flush_followed_by_failure_rolls_back(database, monkeypatch):
    async def check():
        async with database() as (factory, task_id, _):
            before = await snapshot(factory, task_id)
            async with factory() as session:
                service = service_for(session)
                real_rollback = session.rollback
                rollback_calls = 0

                async def tracked_rollback():
                    nonlocal rollback_calls
                    rollback_calls += 1
                    await real_rollback()

                async def fail_after_flush():
                    await session.flush()
                    status = await session.scalar(select(TaskModel.status).where(TaskModel.id == task_id))
                    assert status == Status.RUNNING.value
                    raise RuntimeError("controlled failure after PostgreSQL flush")

                monkeypatch.setattr(session, "commit", fail_after_flush)
                monkeypatch.setattr(session, "rollback", tracked_rollback)
                with pytest.raises(RuntimeError, match="controlled failure"):
                    await service.transition_status(task_id, Status.QUEUED, Status.RUNNING)
                assert rollback_calls == 1
                assert not session.in_transaction()
            assert await snapshot(factory, task_id) == before

    asyncio.run(check())


@pytest.mark.parametrize("winner,loser", [
    (Status.RUNNING, Status.CANCELLED),
    (Status.CANCELLED, Status.RUNNING),
])
def test_competing_transitions_wait_for_row_lock(database, monkeypatch, winner, loser):
    async def check():
        async with database() as (factory, task_id, _):
            before = await snapshot(factory, task_id)
            async with factory() as first, factory() as second, factory() as observer:
                first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
                second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
                assert first_pid != second_pid
                service = service_for(first)
                locked, release = asyncio.Event(), asyncio.Event()
                original_get = service.tasks.get_for_update

                async def hold_lock(key):
                    task = await original_get(key)
                    locked.set()
                    await release.wait()
                    return task

                monkeypatch.setattr(service.tasks, "get_for_update", hold_lock)
                tasks = []
                try:
                    async with asyncio.timeout(10):
                        tasks.append(asyncio.create_task(service.transition_status(task_id, Status.QUEUED, winner)))
                        await locked.wait()
                        tasks.append(asyncio.create_task(
                            service_for(second).transition_status(task_id, Status.QUEUED, loser)
                        ))
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
                        with pytest.raises(StaleTaskStatusError) as exc:
                            await tasks[1]
                        assert exc.value.expected is Status.QUEUED and exc.value.actual is winner
                        assert not first.in_transaction() and not second.in_transaction()
                finally:
                    release.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            after = await snapshot(factory, task_id)
            assert after[:-1] == before[:-1]
            assert after.status == winner.value

    asyncio.run(check())
