from itertools import product

import pytest

from app.domain.execution import ToolCallStatus as Status
from app.domain.tool_call_transitions import InvalidToolCallTransition, validate_tool_call_transition


ALLOWED = {
    (Status.PROPOSED, Status.RUNNING),
    (Status.PROPOSED, Status.DENIED),
    (Status.PROPOSED, Status.FAILED),
    (Status.RUNNING, Status.SUCCEEDED),
    (Status.RUNNING, Status.FAILED),
}


@pytest.mark.parametrize(("current", "target"), tuple(product(Status, repeat=2)))
def test_tool_call_transition_matrix(current, target):
    if (current, target) in ALLOWED:
        assert validate_tool_call_transition(current, target) is None
    else:
        with pytest.raises(InvalidToolCallTransition) as caught:
            validate_tool_call_transition(current, target)
        assert (caught.value.current, caught.value.target) == (current, target)


@pytest.mark.parametrize(("current", "target"), [("proposed", Status.RUNNING), (Status.PROPOSED, "running"), (None, 1)])
def test_tool_call_transition_rejects_untyped_statuses(current, target):
    with pytest.raises(TypeError):
        validate_tool_call_transition(current, target)
