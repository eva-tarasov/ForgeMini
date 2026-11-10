from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkspaceModel
from app.domain.workspace import Workspace


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, workspace: Workspace) -> None:
        self.session.add(WorkspaceModel(id=workspace.id, name=workspace.name))

    async def get(self, workspace_id: UUID) -> Workspace | None:
        model = await self.session.get(WorkspaceModel, workspace_id)
        return Workspace(id=model.id, name=model.name) if model else None

    async def list(self) -> list[Workspace]:
        result = await self.session.scalars(select(WorkspaceModel).order_by(WorkspaceModel.id))
        return [Workspace(id=model.id, name=model.name) for model in result]
