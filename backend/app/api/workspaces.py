from typing import Annotated
from fastapi import APIRouter, Depends, Request, status

from app.schemas.workspace import WorkspaceCreate, WorkspaceResponse
from app.services.workspaces import WorkspaceService

router = APIRouter()


def get_workspace_service(request: Request) -> WorkspaceService:
    return request.app.state.workspace_service


@router.post(
    "/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workspace(
    workspace: WorkspaceCreate,
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> WorkspaceResponse:
    created = service.create_workspace(workspace.name)
    return WorkspaceResponse(id=created.id, name=created.name)


@router.get("/workspaces", response_model=list[WorkspaceResponse])
def list_workspaces(
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> list[WorkspaceResponse]:
    return [
        WorkspaceResponse(id=workspace.id, name=workspace.name)
        for workspace in service.list_workspaces()
    ]
