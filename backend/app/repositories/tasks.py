from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskModel
from app.domain.task import Task


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, task: Task) -> None:
        self.session.add(TaskModel(
            id=task.id,
            workspace_id=task.workspace_id,
            goal=task.goal,
            acceptance_criteria=list(task.acceptance_criteria),
        ))

    async def get(self, task_id: UUID) -> Task | None:
        model = await self.session.get(TaskModel, task_id)
        if model is None:
            return None
        return Task(
            id=model.id,
            workspace_id=model.workspace_id,
            goal=model.goal,
            acceptance_criteria=tuple(model.acceptance_criteria),
        )
