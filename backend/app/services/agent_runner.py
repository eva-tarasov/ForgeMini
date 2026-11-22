from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.services.agent_result import AgentFinalResult


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Snapshot of a task and its prepared run."""

    task_id: UUID
    run_id: UUID
    goal: str
    acceptance_criteria: tuple[str, ...]


class AgentRunner(Protocol):
    async def run(self, request: AgentRunRequest) -> AgentFinalResult:
        ...


class AgentRunnerError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
