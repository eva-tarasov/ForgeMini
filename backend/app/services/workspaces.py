from dataclasses import dataclass
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    name: str


class WorkspaceService:
    def __init__(self) -> None:
        self._workspaces: list[Workspace] = []

    def create_workspace(self, name: str) -> Workspace:
        workspace = Workspace(id=uuid4(), name=name)
        self._workspaces.append(workspace)
        return workspace

    def list_workspaces(self) -> list[Workspace]:
        return list(self._workspaces)

    def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        return next(
            (workspace for workspace in self._workspaces if workspace.id == workspace_id),
            None,
        )
