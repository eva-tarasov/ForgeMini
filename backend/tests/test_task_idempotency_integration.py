"""Opt-in PostgreSQL idempotency checks; use an existing forge_task06_* database."""

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
from app.domain.task import TaskStatus
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.tasks import IdempotencyKeyConflictError, TaskService


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)

GOAL = "synthetic goal"
CRITERIA = ["first", "second"]


@pytest.fixture
def database():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    settings = Settings(postgres_db=name)

    @asynccontextmanager
    async def seeded():
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        workspace_ids = [uuid4(), uuid4()]
        task_ids = set()
        created = False
        try:
            async with asyncio.timeout(30):
                async with factory() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    session.add_all([WorkspaceModel(id=key, name="synthetic idempotency") for key in workspace_ids])
                    await session.flush()
                    created = True  # Also clean up if the commit outcome is uncertain.
                    await session.commit()
                yield factory, workspace_ids, task_ids
        finally:
            try:
                if created:
                    async with asyncio.timeout(10):
                        async with factory.begin() as session:
                            if task_ids:
                                await session.execute(delete(TaskModel).where(
                                    TaskModel.id.in_(task_ids), TaskModel.workspace_id.in_(workspace_ids)
                                ))
                            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id.in_(workspace_ids)))
            finally:
                await asyncio.wait_for(engine.dispose(), timeout=10)

    return seeded


def service_for(session, task_ids):
    repository = TaskRepository(session)
    original_add = repository.add
    original_keyed_add = repository.add_with_idempotency_key

    async def add(task):
        task_ids.add(task.id)
        return await original_add(task)

    async def add_keyed(task, key):
        task_ids.add(task.id)
        return await original_keyed_add(task, key)

    repository.add = add
    repository.add_with_idempotency_key = add_keyed
    return TaskService(session, repository, WorkspaceRepository(session))


async def create(factory, task_ids, workspace_id, key=None, goal=GOAL, criteria=None):
    async with factory() as session:
        task = await service_for(session, task_ids).create_task(
            workspace_id, goal, CRITERIA if criteria is None else criteria, key
        )
        assert not session.in_transaction()
        return task


async def rows(factory, workspace_id):
    async with factory() as session:
        return (await session.execute(
            select(TaskModel.id, TaskModel.workspace_id, TaskModel.goal, TaskModel.acceptance_criteria,
                   TaskModel.status, TaskModel.idempotency_key)
            .where(TaskModel.workspace_id == workspace_id).order_by(TaskModel.id)
        )).all()


def test_first_creation_and_replay(database):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            first = await create(factory, task_ids, workspace, key, " synthetic goal ", [" first ", "second"])
            replay = await create(factory, task_ids, workspace, key, " synthetic goal ", [" first ", "second"])
            unicode_replay = await create(
                factory, task_ids, workspace, key, "\u00a0synthetic goal\u3000", ["\u2003first", "second\u202f"],
            )
            result = await rows(factory, workspace)
            assert first.id == replay.id == unicode_replay.id
            assert len(result) == 1
            assert (result[0].id, result[0].idempotency_key, result[0].goal,
                    result[0].acceptance_criteria, result[0].status) == (
                first.id, key, GOAL, CRITERIA, "queued"
            )

    asyncio.run(check())


@pytest.mark.parametrize("field", ["goal", "criterion"])
@pytest.mark.parametrize("control_first", [True, False], ids=["control-first", "plain-first"])
def test_postgres_replay_rejects_control_separator_difference(database, field, control_first):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            plain = ("Build API", ["criterion"])
            control = ("\x1cBuild API", ["criterion"]) if field == "goal" else ("Build API", ["criterion\x1f"])
            first, second = (control, plain) if control_first else (plain, control)

            created = await create(factory, task_ids, workspace, key, *first)
            before = await rows(factory, workspace)
            assert len(before) == 1
            assert (before[0].goal, before[0].acceptance_criteria) == first
            replay = await create(factory, task_ids, workspace, key, *first)
            assert replay.id == created.id

            async with factory() as session:
                with pytest.raises(IdempotencyKeyConflictError):
                    await service_for(session, task_ids).create_task(workspace, *second, key)
                assert not session.in_transaction()
            assert await rows(factory, workspace) == before

    asyncio.run(check())


@pytest.mark.parametrize("stored", ["canonical", "padded", "invalid"])
def test_historical_keyed_task_replay_uses_stored_payload(database, stored):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            task_id, key = uuid4(), uuid4()
            task_ids.add(task_id)
            goal, criteria = GOAL, CRITERIA
            if stored == "padded":
                goal, criteria = f"\u00a0{GOAL}\u3000", ["\u2003first", "second\u202f"]
            elif stored == "invalid":
                criteria = [None, "second"]
            async with factory.begin() as session:
                session.add(TaskModel(
                    id=task_id, workspace_id=workspace, goal=goal, acceptance_criteria=criteria,
                    status=TaskStatus.RUNNING.value, idempotency_key=key,
                ))
            before = await rows(factory, workspace)
            async with factory() as session:
                service = service_for(session, task_ids)
                if stored == "invalid":
                    with pytest.raises(IdempotencyKeyConflictError):
                        await service.create_task(workspace, GOAL, CRITERIA, key)
                else:
                    original = await service.create_task(workspace, GOAL, CRITERIA, key)
                    assert (original.id, original.status) == (task_id, TaskStatus.RUNNING)
                assert not session.in_transaction()
            assert await rows(factory, workspace) == before

    asyncio.run(check())


@pytest.mark.parametrize("goal,criteria", [("different", CRITERIA), (GOAL, ["second", "first"])])
def test_payload_conflict_rolls_back(database, goal, criteria):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            await create(factory, task_ids, workspace, key)
            before = await rows(factory, workspace)
            async with factory() as session:
                with pytest.raises(IdempotencyKeyConflictError):
                    await service_for(session, task_ids).create_task(workspace, goal, criteria, key)
                assert not session.in_transaction()
            assert len(before) == 1 and await rows(factory, workspace) == before

    asyncio.run(check())


def test_replay_after_transition_preserves_status(database):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            first = await create(factory, task_ids, workspace, key)
            async with factory() as session:
                await service_for(session, task_ids).transition_status(first.id, TaskStatus.QUEUED, TaskStatus.RUNNING)
                assert not session.in_transaction()
            replay = await create(factory, task_ids, workspace, key)
            result = await rows(factory, workspace)
            assert replay.id == first.id and replay.status is TaskStatus.RUNNING
            assert len(result) == 1 and result[0].status == "running"

    asyncio.run(check())


def test_no_key_creates_independent_tasks(database):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            first = await create(factory, task_ids, workspace, goal=" synthetic goal ", criteria=[" first ", "second"])
            second = await create(factory, task_ids, workspace)
            result = await rows(factory, workspace)
            assert first.id != second.id
            assert {row.id for row in result} == {first.id, second.id}
            assert all(row.idempotency_key is None for row in result)
            assert all((row.goal, row.acceptance_criteria) == (GOAL, CRITERIA) for row in result)

    asyncio.run(check())


@pytest.mark.parametrize("keyed", [False, True], ids=["without-key", "with-key"])
def test_invalid_direct_task_input_does_not_create_rows(database, keyed):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4() if keyed else None
            async with factory() as session:
                service = service_for(session, task_ids)
                for goal, criteria, code in (
                    (" ", CRITERIA, "invalid_task_goal"),
                    (GOAL, [" "], "invalid_task_acceptance_criterion"),
                    (GOAL, ["c" * 301], "invalid_task_acceptance_criterion"),
                ):
                    with pytest.raises(ValueError, match=f"^{code}$"):
                        await service.create_task(workspace, goal, criteria, key)
                    assert not session.in_transaction()
            assert await rows(factory, workspace) == []

    asyncio.run(check())


def test_historical_task_is_read_without_normalization(database):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            task_id = uuid4()
            task_ids.add(task_id)
            goal, criteria = "  historical goal  ", [" ", "c" * 301]
            async with factory.begin() as session:
                session.add(TaskModel(id=task_id, workspace_id=workspace, goal=goal, acceptance_criteria=criteria))
            before = await rows(factory, workspace)
            async with factory() as session:
                loaded = await service_for(session, task_ids).get_task(task_id)
                assert (loaded.id, loaded.goal, loaded.acceptance_criteria) == (task_id, goal, tuple(criteria))
            assert await rows(factory, workspace) == before

    asyncio.run(check())


def test_same_key_in_different_workspaces(database):
    async def check():
        async with database() as (factory, (first_workspace, second_workspace), task_ids):
            key = uuid4()
            first = await create(factory, task_ids, first_workspace, key)
            second = await create(factory, task_ids, second_workspace, key)
            assert first.id != second.id
            for workspace, task in [(first_workspace, first), (second_workspace, second)]:
                result = await rows(factory, workspace)
                assert len(result) == 1 and result[0].id == task.id and result[0].idempotency_key == key

    asyncio.run(check())


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_same_key_waits_for_insert(database, monkeypatch, conflict):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            async with factory() as first, factory() as second, factory() as observer:
                first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
                second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
                assert first_pid != second_pid
                first_service = service_for(first, task_ids)
                second_service = service_for(second, task_ids)
                inserted, release = asyncio.Event(), asyncio.Event()
                original_add = first_service.tasks.add_with_idempotency_key

                async def hold_after_insert(task, task_key):
                    result = await original_add(task, task_key)
                    assert result
                    inserted.set()
                    await release.wait()
                    return result

                monkeypatch.setattr(first_service.tasks, "add_with_idempotency_key", hold_after_insert)
                tasks = []
                try:
                    async with asyncio.timeout(10):
                        tasks.append(asyncio.create_task(first_service.create_task(workspace, GOAL, CRITERIA, key)))
                        await inserted.wait()
                        tasks.append(asyncio.create_task(second_service.create_task(
                            workspace, "different" if conflict else GOAL, CRITERIA, key
                        )))
                        while True:
                            blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid})
                            if first_pid in blockers:
                                break
                            assert not tasks[1].done(), "Competing insert did not wait"
                            await asyncio.sleep(0.01)
                        assert not tasks[0].done() and not tasks[1].done()
                        release.set()
                        winner = await tasks[0]
                        if conflict:
                            with pytest.raises(IdempotencyKeyConflictError):
                                await tasks[1]
                        else:
                            replay = await tasks[1]
                            assert replay.id == winner.id
                        assert not first.in_transaction() and not second.in_transaction()
                finally:
                    release.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            result = await rows(factory, workspace)
            assert len(result) == 1
            assert (result[0].id, result[0].idempotency_key, result[0].goal,
                    result[0].acceptance_criteria, result[0].status) == (winner.id, key, GOAL, CRITERIA, "queued")

    asyncio.run(check())


def test_rollback_releases_key(database, monkeypatch):
    async def check():
        async with database() as (factory, (workspace, _), task_ids):
            key = uuid4()
            async with factory() as session:
                service = service_for(session, task_ids)
                original_add = service.tasks.add_with_idempotency_key
                real_rollback = session.rollback
                failed_ids = []
                rollbacks = 0

                async def fail_after_insert(task, task_key):
                    assert await original_add(task, task_key)
                    failed_ids.append(task.id)
                    assert await session.scalar(select(TaskModel.id).where(TaskModel.id == task.id)) == task.id
                    raise RuntimeError("controlled failure after insert")

                async def track_rollback():
                    nonlocal rollbacks
                    rollbacks += 1
                    await real_rollback()

                monkeypatch.setattr(service.tasks, "add_with_idempotency_key", fail_after_insert)
                monkeypatch.setattr(session, "rollback", track_rollback)
                with pytest.raises(RuntimeError, match="controlled failure"):
                    await service.create_task(workspace, GOAL, CRITERIA, key)
                assert rollbacks == 1 and not session.in_transaction() and len(failed_ids) == 1
            assert await rows(factory, workspace) == []
            committed = await create(factory, task_ids, workspace, key)
            result = await rows(factory, workspace)
            assert committed.id != failed_ids[0]
            assert len(result) == 1 and result[0].id == committed.id and result[0].idempotency_key == key

    asyncio.run(check())
