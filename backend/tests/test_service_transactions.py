import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.workspace import Workspace
from app.services.tasks import TaskService, WorkspaceNotFoundError
from app.services.workspaces import WorkspaceService
from fake_app import create_test_app


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
