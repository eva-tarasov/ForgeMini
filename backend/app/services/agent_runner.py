from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from app.services.agent_result import AgentFinalResult

if TYPE_CHECKING:
    from app.services.approval_outcome import ApprovalOutcomePort


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


@runtime_checkable
class ApprovalOutcomeRunner(AgentRunner, Protocol):
    async def run_with_approval_outcome(
        self, request: AgentRunRequest, *, outcome: "ApprovalOutcomePort",
    ) -> AgentFinalResult:
        ...


class AgentRunnerError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
