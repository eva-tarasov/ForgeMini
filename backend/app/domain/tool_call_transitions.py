"""Pure ToolCall transitions, independent of AgentRun and Task status."""

from types import MappingProxyType

from app.domain.execution import ToolCallStatus


class InvalidToolCallTransition(ValueError):
    def __init__(self, current: ToolCallStatus, target: ToolCallStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid ToolCall transition: {current.value} -> {target.value}")


_ALLOWED = MappingProxyType({
    ToolCallStatus.PROPOSED: frozenset({
        ToolCallStatus.RUNNING, ToolCallStatus.DENIED, ToolCallStatus.FAILED,
    }),
    ToolCallStatus.RUNNING: frozenset({ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED}),
    ToolCallStatus.SUCCEEDED: frozenset(),
    ToolCallStatus.FAILED: frozenset(),
    ToolCallStatus.DENIED: frozenset(),
})


def validate_tool_call_transition(current: ToolCallStatus, target: ToolCallStatus) -> None:
    if not isinstance(current, ToolCallStatus) or not isinstance(target, ToolCallStatus):
        raise TypeError("ToolCall statuses must be ToolCallStatus members")
    if target not in _ALLOWED[current]:
        raise InvalidToolCallTransition(current, target)
