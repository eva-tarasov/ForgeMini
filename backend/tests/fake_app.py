"""Isolated domain fakes for HTTP tests; PostgreSQL is tested separately."""

from app.api.tasks import get_task_service
from app.api.workspaces import get_workspace_service
from app.main import create_app
from app.services.tasks import TaskService
from app.services.workspaces import WorkspaceService


class FakeSession:
    async def commit(self):
        pass

    async def rollback(self):
        pass


class FakeWorkspaceRepository:
    def __init__(self):
        self.values = {}

    async def add(self, workspace):
        self.values[workspace.id] = workspace

    async def get(self, workspace_id):
        return self.values.get(workspace_id)

    async def list(self):
        return [self.values[key] for key in sorted(self.values)]


class FakeTaskRepository:
    def __init__(self):
        self.values = {}

    async def add(self, task):
        self.values[task.id] = task

    async def get(self, task_id):
        return self.values.get(task_id)


def create_test_app():
    app = create_app(session_factory=lambda: None)
    workspaces = FakeWorkspaceRepository()
    tasks = FakeTaskRepository()
    app.dependency_overrides[get_workspace_service] = lambda: WorkspaceService(FakeSession(), workspaces)
    app.dependency_overrides[get_task_service] = lambda: TaskService(FakeSession(), tasks, workspaces)
    return app
