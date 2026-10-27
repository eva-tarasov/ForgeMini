from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, status
from app.schemas.task import TaskCreate, TaskResponse
from app.services.tasks import TaskService, WorkspaceNotFoundError

router = APIRouter()


def get_task_service(request: Request) -> TaskService:
    return request.app.state.task_service


@router.post("/tasks",
             response_model=TaskResponse,
             status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskResponse:
    try:
        task = service.create_task(
            workspace_id=payload.workspace_id,
            goal=payload.goal,
            acceptance_criteria=payload.acceptance_criteria,
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace not found") from exc
    return TaskResponse.model_validate(task, from_attributes=True)


@router.get("/tasks/{task_id}",
            response_model=TaskResponse)
def get_task(
    task_id: UUID,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskResponse:
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.model_validate(task, from_attributes=True)
