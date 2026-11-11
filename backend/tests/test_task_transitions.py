from itertools import product

import pytest

from app.domain.execution import AgentRunStatus
from app.domain.task import TaskStatus as Status
from app.domain.task_transitions import InvalidTaskTransition, validate_task_transition


ALLOWED = {
    (Status.QUEUED, Status.RUNNING), (Status.QUEUED, Status.FAILED), (Status.QUEUED, Status.CANCELLED),
    (Status.RUNNING, Status.WAITING_APPROVAL), (Status.RUNNING, Status.VERIFYING),
    (Status.RUNNING, Status.FAILED), (Status.RUNNING, Status.CANCELLED),
    (Status.WAITING_APPROVAL, Status.RUNNING), (Status.WAITING_APPROVAL, Status.FAILED),
    (Status.WAITING_APPROVAL, Status.CANCELLED),
    (Status.VERIFYING, Status.RUNNING), (Status.VERIFYING, Status.SUCCEEDED),
    (Status.VERIFYING, Status.FAILED), (Status.VERIFYING, Status.CANCELLED),
}


@pytest.mark.parametrize("current,target", list(product(Status, repeat=2)))
def test_complete_transition_graph(current, target):
    if (current, target) in ALLOWED:
        assert validate_task_transition(current, target) is None
    else:
        with pytest.raises(InvalidTaskTransition) as exc:
            validate_task_transition(current, target)
        assert exc.value.current is current and exc.value.target is target
        assert current.value in str(exc.value) and target.value in str(exc.value)


@pytest.mark.parametrize("invalid", ["queued", None, 1, object(), AgentRunStatus.QUEUED])
def test_invalid_status_types(invalid):
    with pytest.raises(TypeError, match="current must be a TaskStatus"):
        validate_task_transition(invalid, Status.RUNNING)
    with pytest.raises(TypeError, match="target must be a TaskStatus"):
        validate_task_transition(Status.QUEUED, invalid)
