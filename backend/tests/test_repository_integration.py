"""Opt-in HTTP and repository checks against a dedicated migrated database."""

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main as main_module
from app.core.settings import Settings
from app.db.models import TaskModel, WorkspaceModel
from app.db.session import create_engine
from app.repositories.tasks import TaskRepository


TEST_DATABASE = os.environ.get("FORGE_TASK04_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DATABASE, reason="set FORGE_TASK04_TEST_DB to opt in")


@pytest.fixture
def test_data():
    if not TEST_DATABASE or not re.fullmatch(r"forge_task04_[a-z0-9_]+", TEST_DATABASE):
        pytest.fail("FORGE_TASK04_TEST_DB must be a dedicated forge_task04_* database", pytrace=False)
    settings = Settings(postgres_db=TEST_DATABASE)
    created = {"workspaces": [], "tasks": []}
    yield settings, created

    async def cleanup():
        engine = create_engine(settings)
        try:
            async with engine.begin() as connection:
                if created["tasks"]:
                    await connection.execute(delete(TaskModel).where(TaskModel.id.in_(created["tasks"])))
                if created["workspaces"]:
                    await connection.execute(delete(WorkspaceModel).where(WorkspaceModel.id.in_(created["workspaces"])))
        finally:
            await engine.dispose()

    asyncio.run(cleanup())


def test_http_persistence_rollback_and_lifecycle(test_data, monkeypatch):
    settings, created = test_data
    opened, closed, disposed = [], [], []
    real_dispose = AsyncEngine.dispose

    class TrackingSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        async def close(self):
            try:
                await super().close()
            finally:
                closed.append(self)

    async def tracking_dispose(engine, *args, **kwargs):
        try:
            await real_dispose(engine, *args, **kwargs)
        finally:
            disposed.append(engine)

    monkeypatch.setattr(AsyncEngine, "dispose", tracking_dispose)
    monkeypatch.setattr(main_module, "create_session_factory", lambda engine: async_sessionmaker(
        engine, class_=TrackingSession, expire_on_commit=False
    ))

    app = main_module.create_app(settings=settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        workspace_response = client.post("/workspaces", json={"name": f"task04-{uuid4()}"})
        assert workspace_response.status_code == 201
        workspace = workspace_response.json()
        created["workspaces"].append(UUID(workspace["id"]))
        assert set(workspace) == {"id", "name", "source_path", "test_command", "image", "setup_command"}
        workspaces = client.get("/workspaces")
        assert workspaces.status_code == 200
        assert workspace in workspaces.json()
        assert [item["id"] for item in workspaces.json()] == sorted(item["id"] for item in workspaces.json())

        payload = {"workspace_id": workspace["id"], "goal": " Build API ", "acceptance_criteria": [" Works "]}
        task_response = client.post("/tasks", json=payload)
        assert task_response.status_code == 201
        task = task_response.json()
        created["tasks"].append(UUID(task["id"]))
        assert task == {"id": task["id"], "workspace_id": workspace["id"],
                        "goal": "Build API", "acceptance_criteria": ["Works"]}
        assert client.get(f"/tasks/{task['id']}").json() == task
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: client.get("/workspaces"), range(4)))
        assert all(response.status_code == 200 for response in responses)
        missing = client.post("/tasks", json={**payload, "workspace_id": str(uuid4())})
        assert missing.status_code == 404
        assert missing.json() == {"error": {"code": "WORKSPACE_NOT_FOUND", "message": "Workspace not found", "details": []}}

        original_add = TaskRepository.add
        failed_ids = []

        async def fail_after_flush(repository, new_task):
            failed_ids.append(new_task.id)
            created["tasks"].append(new_task.id)
            await original_add(repository, new_task)
            await repository.session.flush()
            raise RuntimeError("simulated failure after insert")

        monkeypatch.setattr(TaskRepository, "add", fail_after_flush)
        assert client.post("/tasks", json=payload).status_code == 500
        assert len(failed_ids) == 1
        assert client.get(f"/tasks/{failed_ids[0]}").status_code == 404
    assert len(disposed) == 1

    with TestClient(main_module.create_app(settings=settings)) as client:
        assert workspace in client.get("/workspaces").json()
        assert client.get(f"/tasks/{task['id']}").json() == task
        assert client.get(f"/tasks/{failed_ids[0]}").status_code == 404
    assert len(disposed) == 2
    assert len(opened) >= 9
    assert len({id(session) for session in opened}) == len(opened)
    assert len(closed) == len(opened)
    assert {id(session) for session in closed} == {id(session) for session in opened}
