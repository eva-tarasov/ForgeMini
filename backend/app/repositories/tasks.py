from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskModel
from app.domain.task import Task, TaskStatus


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, task: Task) -> None:
        self.session.add(TaskModel(
            id=task.id,
            workspace_id=task.workspace_id,
            goal=task.goal,
            acceptance_criteria=list(task.acceptance_criteria),
            status=task.status.value,
        ))

    async def add_with_idempotency_key(self, task: Task, key: UUID) -> bool:
        statement = (
            insert(TaskModel)
            .values(id=task.id, workspace_id=task.workspace_id, goal=task.goal,
                    acceptance_criteria=list(task.acceptance_criteria), status=task.status.value,
                    idempotency_key=key)
            .on_conflict_do_nothing(constraint="uq_tasks_workspace_idempotency_key")
            .returning(TaskModel.id)
        )
        return await self.session.scalar(statement) is not None

    async def get_by_idempotency_key(self, workspace_id: UUID, key: UUID) -> Task | None:
        # READ COMMITTED gives this separate statement a fresh snapshot after a conflicting insert.
        statement = select(TaskModel).where(
            TaskModel.workspace_id == workspace_id, TaskModel.idempotency_key == key
        )
        model = await self.session.scalar(statement)
        return self._to_task(model) if model is not None else None

    async def get(self, task_id: UUID) -> Task | None:
        model = await self.session.get(TaskModel, task_id)
        if model is None:
            return None
        return self._to_task(model)

    @staticmethod
    def _to_task(model: TaskModel) -> Task:
        return Task(
            id=model.id,
            workspace_id=model.workspace_id,
            goal=model.goal,
            acceptance_criteria=tuple(model.acceptance_criteria),
            status=TaskStatus(model.status),
        )

    async def get_for_update(self, task_id: UUID) -> TaskModel | None:
        statement = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        return await self.session.scalar(statement)
