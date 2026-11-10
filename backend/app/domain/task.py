from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Task:
    id: UUID
    workspace_id: UUID
    goal: str
    acceptance_criteria: tuple[str, ...]
