import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import app.main as main_module
from app.db.models import WorkspaceModel
from app.domain.workspace import Workspace
from app.repositories.workspaces import WorkspaceRepository
from app.services.tasks import TaskService, WorkspaceNotFoundError
from app.services.workspaces import WorkspaceService
from fake_app import create_test_app
from test_task_idempotency_integration import database as postgres_database


def test_fake_app_does_not_read_database_settings(monkeypatch):
    def fail_if_used():
        raise AssertionError("Settings must not be loaded for fake-backed tests")

    monkeypatch.setattr(main_module, "Settings", fail_if_used)
    with TestClient(create_test_app()) as client:
        assert client.post("/workspaces", json={"name": "test"}).status_code == 201


def test_workspace_commit_failure_rolls_back_and_propagates():
    session = SimpleNamespace(commit=AsyncMock(side_effect=RuntimeError("commit failed")), rollback=AsyncMock())
    repository = SimpleNamespace(add=AsyncMock())

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(WorkspaceService(session, repository).create_workspace("test"))

    repository.add.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()


def test_missing_workspace_rolls_back_without_creating_task():
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    tasks = SimpleNamespace(add=AsyncMock())
    workspaces = SimpleNamespace(get=AsyncMock(return_value=None))

    with pytest.raises(WorkspaceNotFoundError):
        asyncio.run(TaskService(session, tasks, workspaces).create_task(uuid4(), "test", ["ok"]))

    tasks.add.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_task_add_failure_rolls_back_without_commit():
    workspace = Workspace(id=uuid4(), name="test")
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    tasks = SimpleNamespace(add=AsyncMock(side_effect=RuntimeError("write failed")))
    workspaces = SimpleNamespace(get=AsyncMock(return_value=workspace))

    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(TaskService(session, tasks, workspaces).create_task(workspace.id, "test", ["ok"]))

    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("point", ["add", "commit"])
def test_workspace_cancellation_rolls_back_and_propagates_same_exception(point):
    original = asyncio.CancelledError("original cancellation")
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), close=AsyncMock())
    repository = SimpleNamespace(add=AsyncMock())
    interrupted = repository.add if point == "add" else session.commit
    interrupted.side_effect = original

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(WorkspaceService(session, repository).create_workspace("test"))

    assert caught.value is original
    repository.add.assert_awaited_once()
    assert session.commit.await_count == (0 if point == "add" else 1)
    session.rollback.assert_awaited_once()
    session.close.assert_not_awaited()


@pytest.mark.parametrize("primary", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("cleanup_failure", [RuntimeError, asyncio.CancelledError])
def test_workspace_rollback_failure_preserves_primary_exception(primary, cleanup_failure):
    original = primary("original error")
    session = SimpleNamespace(
        commit=AsyncMock(side_effect=original),
        rollback=AsyncMock(side_effect=cleanup_failure("synthetic private cleanup diagnostic")),
        close=AsyncMock(),
    )
    repository = SimpleNamespace(add=AsyncMock())

    with pytest.raises(primary) as caught:
        asyncio.run(WorkspaceService(session, repository).create_workspace("test"))

    assert caught.value is original
    assert caught.value.__notes__ == ["workspace_rollback_failed"]
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()
    session.close.assert_not_awaited()


@pytest.mark.skipif(os.getenv("FORGE_TASK06_TEST_DB") is None, reason="PostgreSQL opt-in")
@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_workspace_postgres_cancellation_respects_commit_boundary(postgres_database, monkeypatch, committed):
    async def check():
        async with postgres_database() as (factory, (existing_workspace, _), _task_ids):
            created_ids = []
            try:
                async with factory() as session:
                    repository = WorkspaceRepository(session)
                    original_add, original_commit = repository.add, session.commit
                    commit_boundary = asyncio.Event()
                    rollback = AsyncMock(wraps=session.rollback)

                    async def capture_workspace(workspace):
                        created_ids.append(workspace.id)
                        await original_add(workspace)

                    async def pause_at_commit():
                        await session.flush()
                        if committed:
                            await original_commit()
                        commit_boundary.set()
                        await asyncio.Event().wait()

                    with monkeypatch.context() as changes:
                        changes.setattr(repository, "add", capture_workspace)
                        changes.setattr(session, "commit", pause_at_commit)
                        changes.setattr(session, "rollback", rollback)
                        creation = asyncio.create_task(WorkspaceService(session, repository).create_workspace("synthetic"))
                        try:
                            await asyncio.wait_for(commit_boundary.wait(), 3)
                            creation.cancel("workspace cancellation")
                            with pytest.raises(asyncio.CancelledError, match="workspace cancellation"):
                                await asyncio.wait_for(creation, 3)
                        finally:
                            if not creation.done():
                                creation.cancel()
                            await asyncio.gather(creation, return_exceptions=True)
                        rollback.assert_awaited_once()
                        assert not session.in_transaction()

                    assert await repository.get(existing_workspace) is not None
                (created_id,) = created_ids
                async with factory() as observer:
                    observed = await WorkspaceRepository(observer).get(created_id)
                    assert (observed is not None) == committed
            finally:
                if created_ids:
                    async with factory.begin() as cleanup:
                        assert await cleanup.scalar(text("SELECT current_database()")) == os.environ["FORGE_TASK06_TEST_DB"]
                        await cleanup.execute(delete(WorkspaceModel).where(WorkspaceModel.id.in_(created_ids)))

    asyncio.run(check())
