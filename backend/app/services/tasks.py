from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.task import Task, TaskStatus
from app.domain.task_transitions import validate_task_transition
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository


class WorkspaceNotFoundError(Exception):
    pass


class IdempotencyKeyConflictError(Exception):
    pass


class IdempotencyRecordNotFoundError(RuntimeError):
    pass


class TaskNotFoundError(Exception):
    pass


class StaleTaskStatusError(Exception):
    def __init__(self, expected: TaskStatus, actual: TaskStatus) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Expected {expected.value}, found {actual.value}")


class TaskVerificationEvidenceRequiredError(Exception):
    pass


class TaskService:
    def __init__(
        self, session: AsyncSession, tasks: TaskRepository, workspaces: WorkspaceRepository
    ) -> None:
        self.session = session
        self.tasks = tasks
        self.workspaces = workspaces

    async def create_task(
        self, workspace_id: UUID, goal: str, acceptance_criteria: list[str],
        idempotency_key: UUID | None = None,
    ) -> Task:
        try:
            if await self.workspaces.get(workspace_id) is None:
                raise WorkspaceNotFoundError(workspace_id)
            if idempotency_key is not None:
                goal = goal.strip()
                acceptance_criteria = [criterion.strip() for criterion in acceptance_criteria]
            task = Task(
                id=uuid4(), workspace_id=workspace_id, goal=goal,
                acceptance_criteria=tuple(acceptance_criteria),
            )
            if idempotency_key is None:
                await self.tasks.add(task)
            elif not await self.tasks.add_with_idempotency_key(task, idempotency_key):
                existing = await self.tasks.get_by_idempotency_key(workspace_id, idempotency_key)
                if existing is None:
                    raise IdempotencyRecordNotFoundError("Conflicting task was not found")
                if (existing.goal.strip() != goal or
                        tuple(criterion.strip() for criterion in existing.acceptance_criteria) != task.acceptance_criteria):
                    raise IdempotencyKeyConflictError("Idempotency key already used with different task data")
                task = existing
            await self.session.commit()
            return task
        except BaseException:
            await self.session.rollback()
            raise

    async def get_task(self, task_id: UUID) -> Task | None:
        return await self.tasks.get(task_id)

    async def transition_status(
        self, task_id: UUID, expected_status: TaskStatus, target_status: TaskStatus
    ) -> None:
        try:
            if not isinstance(task_id, UUID):
                raise TypeError("task_id must be a UUID")
            if not isinstance(expected_status, TaskStatus):
                raise TypeError("expected_status must be a TaskStatus")
            if not isinstance(target_status, TaskStatus):
                raise TypeError("target_status must be a TaskStatus")
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            actual = TaskStatus(task.status)
            if actual != expected_status:
                raise StaleTaskStatusError(expected_status, actual)
            validate_task_transition(actual, target_status)
            if target_status is TaskStatus.SUCCEEDED:
                raise TaskVerificationEvidenceRequiredError("Independent verification evidence is required")
            task.status = target_status.value
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
