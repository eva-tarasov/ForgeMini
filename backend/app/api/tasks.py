from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.dependencies import get_session
from app.api.errors import ApiError
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.schemas.task import TaskCreate, TaskResponse
from app.services.tasks import IdempotencyKeyConflictError, TaskService, WorkspaceNotFoundError

router = APIRouter()


def get_task_service(session: Annotated[AsyncSession, Depends(get_session)]) -> TaskService:
    return TaskService(session, TaskRepository(session), WorkspaceRepository(session))


@router.post("/tasks",
             response_model=TaskResponse,
             status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    service: Annotated[TaskService, Depends(get_task_service)],
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> TaskResponse:
    try:
        task = await service.create_task(
            workspace_id=payload.workspace_id,
            goal=payload.goal,
            acceptance_criteria=payload.acceptance_criteria,
            idempotency_key=idempotency_key,
        )
    except WorkspaceNotFoundError as exc:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "Workspace not found") from exc
    except IdempotencyKeyConflictError as exc:
        raise ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", "Idempotency key already used with different task data") from exc
    return TaskResponse.model_validate(task, from_attributes=True)


@router.get("/tasks/{task_id}",
            response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskResponse:
    task = await service.get_task(task_id)
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    return TaskResponse.model_validate(task, from_attributes=True)
