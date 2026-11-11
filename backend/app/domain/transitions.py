"""Pure AgentRun transition rules; persistence must enforce additional invariants."""

from types import MappingProxyType

from app.domain.execution import AgentRunStatus


class InvalidAgentRunTransition(ValueError):
    def __init__(self, current: AgentRunStatus, target: AgentRunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid AgentRun transition: {current.value} -> {target.value}")


_ALLOWED = MappingProxyType({
    AgentRunStatus.QUEUED: frozenset({AgentRunStatus.RUNNING, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}),
    AgentRunStatus.RUNNING: frozenset({AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.VERIFYING, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}),
    AgentRunStatus.WAITING_APPROVAL: frozenset({AgentRunStatus.RUNNING, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}),
    AgentRunStatus.VERIFYING: frozenset({AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}),
    AgentRunStatus.SUCCEEDED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
})


def validate_agent_run_transition(current: AgentRunStatus, target: AgentRunStatus) -> None:
    """Validate the graph only; success also requires independent verification evidence."""
    if not isinstance(current, AgentRunStatus):
        raise TypeError("current must be an AgentRunStatus")
    if not isinstance(target, AgentRunStatus):
        raise TypeError("target must be an AgentRunStatus")
    if target not in _ALLOWED[current]:
        raise InvalidAgentRunTransition(current, target)
