from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkspaceModel
from app.domain.workspace import Workspace


def _to_domain(model: WorkspaceModel) -> Workspace:
    return Workspace(id=model.id, name=model.name, source_path=model.source_path,
                     test_command=model.test_command, image=model.image,
                     setup_command=model.setup_command)


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, workspace: Workspace) -> None:
        self.session.add(WorkspaceModel(
            id=workspace.id, name=workspace.name, source_path=workspace.source_path,
            test_command=workspace.test_command, image=workspace.image,
            setup_command=workspace.setup_command))

    async def get(self, workspace_id: UUID) -> Workspace | None:
        model = await self.session.get(WorkspaceModel, workspace_id)
        return _to_domain(model) if model else None

    async def list(self) -> list[Workspace]:
        result = await self.session.scalars(select(WorkspaceModel).order_by(WorkspaceModel.id))
        return [_to_domain(model) for model in result]
