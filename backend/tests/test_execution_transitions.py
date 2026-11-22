from itertools import product

import pytest

from app.domain.execution import AgentRunStatus as Status
from app.domain.transitions import InvalidAgentRunTransition, validate_agent_run_transition


ALLOWED = {
    (Status.QUEUED, Status.RUNNING), (Status.QUEUED, Status.FAILED), (Status.QUEUED, Status.CANCELLED),
    (Status.RUNNING, Status.VERIFYING),
    (Status.RUNNING, Status.FAILED), (Status.RUNNING, Status.CANCELLED),
    (Status.VERIFYING, Status.SUCCEEDED), (Status.VERIFYING, Status.FAILED),
    (Status.VERIFYING, Status.CANCELLED),
}


@pytest.mark.parametrize("current,target", sorted(ALLOWED, key=lambda pair: (pair[0].value, pair[1].value)))
def test_allowed_transitions(current, target):
    assert validate_agent_run_transition(current, target) is None


@pytest.mark.parametrize("current,target", [pair for pair in product(Status, repeat=2) if pair not in ALLOWED])
def test_all_other_transitions_are_rejected(current, target):
    with pytest.raises(InvalidAgentRunTransition) as exc:
        validate_agent_run_transition(current, target)
    assert exc.value.current is current
    assert exc.value.target is target
    assert current.value in str(exc.value) and target.value in str(exc.value)


@pytest.mark.parametrize("invalid", ["queued", "invalid", "", None, 1, object()])
def test_invalid_current_is_rejected(invalid):
    with pytest.raises(TypeError, match="current must be an AgentRunStatus"):
        validate_agent_run_transition(invalid, Status.RUNNING)


@pytest.mark.parametrize("invalid", ["running", "invalid", "", None, 1, object()])
def test_invalid_target_is_rejected(invalid):
    with pytest.raises(TypeError, match="target must be an AgentRunStatus"):
        validate_agent_run_transition(Status.QUEUED, invalid)
