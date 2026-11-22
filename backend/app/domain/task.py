from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Task:
    id: UUID
    workspace_id: UUID
    goal: str
    acceptance_criteria: tuple[str, ...]
    status: TaskStatus = TaskStatus.QUEUED
