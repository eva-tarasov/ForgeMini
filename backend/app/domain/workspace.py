from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    name: str
    # Run configuration (ForgeMini). None for workspaces created before it existed.
    source_path: str | None = None
    test_command: str | None = None
    image: str | None = None
    setup_command: str | None = None   # opt-in setup phase with network before the sandbox (ADR-004)
