from dataclasses import dataclass
from uuid import UUID, uuid4

from app.services.workspaces import WorkspaceService


class WorkspaceNotFoundError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Task:
    id: UUID
    workspace_id: UUID
    goal: str
    acceptance_criteria: tuple[str, ...]


class TaskService:
    def __init__(self, workspace_service: WorkspaceService) -> None:
        self._workspace_service = workspace_service
        self._tasks: dict[UUID, Task] = {}

    def create_task(
        self,
        workspace_id: UUID,
        goal: str,
        acceptance_criteria: list[str],
    ) -> Task:
        if self._workspace_service.get_workspace(workspace_id) is None:
            raise WorkspaceNotFoundError(workspace_id)

        task = Task(
            id=uuid4(),
            workspace_id=workspace_id,
            goal=goal,
            acceptance_criteria=tuple(acceptance_criteria),
        )
        self._tasks[task.id] = task
        return task

    def get_task(self, task_id: UUID) -> Task | None:
        return self._tasks.get(task_id)
