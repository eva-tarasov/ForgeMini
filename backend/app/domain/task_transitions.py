"""Pure Task transition rules; persistence must enforce additional invariants."""

from types import MappingProxyType

from app.domain.task import TaskStatus


class InvalidTaskTransition(ValueError):
    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid Task transition: {current.value} -> {target.value}")


_ALLOWED = MappingProxyType({
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
})


def validate_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Validate the graph only; success also requires independent evidence."""
    if not isinstance(current, TaskStatus):
        raise TypeError("current must be a TaskStatus")
    if not isinstance(target, TaskStatus):
        raise TypeError("target must be a TaskStatus")
    if target not in _ALLOWED[current]:
        raise InvalidTaskTransition(current, target)
