from typing import Annotated
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.repositories.workspaces import WorkspaceRepository
from app.schemas.workspace import WorkspaceCreate, WorkspaceResponse
from app.services.workspaces import WorkspaceService

router = APIRouter()


def get_workspace_service(session: Annotated[AsyncSession, Depends(get_session)]) -> WorkspaceService:
    return WorkspaceService(session, WorkspaceRepository(session))


@router.post(
    "/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    workspace: WorkspaceCreate,
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> WorkspaceResponse:
    created = await service.create_workspace(workspace.name)
    return WorkspaceResponse(id=created.id, name=created.name)


@router.get("/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> list[WorkspaceResponse]:
    return [
        WorkspaceResponse(id=workspace.id, name=workspace.name)
        for workspace in await service.list_workspaces()
    ]
