from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.task import Task
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository


class WorkspaceNotFoundError(Exception):
    pass


class TaskService:
    def __init__(
        self, session: AsyncSession, tasks: TaskRepository, workspaces: WorkspaceRepository
    ) -> None:
        self.session = session
        self.tasks = tasks
        self.workspaces = workspaces

    async def create_task(
        self, workspace_id: UUID, goal: str, acceptance_criteria: list[str]
    ) -> Task:
        try:
            if await self.workspaces.get(workspace_id) is None:
                raise WorkspaceNotFoundError(workspace_id)
            task = Task(
                id=uuid4(), workspace_id=workspace_id, goal=goal,
                acceptance_criteria=tuple(acceptance_criteria),
            )
            await self.tasks.add(task)
            await self.session.commit()
            return task
        except Exception:
            await self.session.rollback()
            raise

    async def get_task(self, task_id: UUID) -> Task | None:
        return await self.tasks.get(task_id)
