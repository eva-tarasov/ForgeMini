from dataclasses import dataclass
from uuid import UUID

from app.domain.capabilities import Capability
from app.permissions.paths import parse_logical_path


@dataclass(frozen=True, slots=True)
class PathScope:
    path: str
    recursive: bool = False

    def __post_init__(self) -> None:
        if type(self.recursive) is not bool:
            raise TypeError("Path scope recursive flag must be a bool")
        parse_logical_path(self.path)


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """In-memory grant snapshot supplied by trusted backend code."""

    run_id: UUID
    workspace_id: UUID
    capabilities: frozenset[Capability]
    read_scopes: tuple[PathScope, ...] = ()
    write_scopes: tuple[PathScope, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not isinstance(self.workspace_id, UUID):
            raise TypeError("Run and workspace IDs must be UUIDs")
        if type(self.capabilities) is not frozenset or any(
            not isinstance(capability, Capability) for capability in self.capabilities
        ):
            raise TypeError("Capabilities must be a frozenset of Capability members")
        for scopes in (self.read_scopes, self.write_scopes):
            if type(scopes) is not tuple or any(type(scope) is not PathScope for scope in scopes):
                raise TypeError("Read and write scopes must be tuples of PathScope")
