from asyncio import CancelledError
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workspace import Workspace
from app.repositories.workspaces import WorkspaceRepository


class WorkspaceService:
    def __init__(self, session: AsyncSession, repository: WorkspaceRepository) -> None:
        self.session = session
        self.repository = repository

    async def create_workspace(self, name: str) -> Workspace:
        workspace = Workspace(id=uuid4(), name=name)
        try:
            await self.repository.add(workspace)
            await self.session.commit()
        except BaseException as error:
            try:
                await self.session.rollback()
            except (Exception, CancelledError):
                error.add_note("workspace_rollback_failed")
            raise
        return workspace

    async def list_workspaces(self) -> list[Workspace]:
        return await self.repository.list()

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        return await self.repository.get(workspace_id)
