import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.workspace import Workspace
from app.services.tasks import TaskService


@pytest.fixture
def task_service():
    workspace = Workspace(id=uuid4(), name="test")
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    tasks = SimpleNamespace(add=AsyncMock(), add_with_idempotency_key=AsyncMock(return_value=True))
    workspaces = SimpleNamespace(get=AsyncMock(return_value=workspace))
    return TaskService(session, tasks, workspaces), workspace.id


@pytest.mark.parametrize("keyed", [False, True], ids=["without-key", "with-key"])
@pytest.mark.parametrize("goal, criteria, code", [
    pytest.param("", ["valid"], "invalid_task_goal", id="empty-goal"),
    pytest.param("   ", ["valid"], "invalid_task_goal", id="blank-goal"),
    pytest.param(" " + "g" * 501 + " ", ["valid"], "invalid_task_goal", id="long-goal"),
    pytest.param(42, ["valid"], "invalid_task_goal", id="goal-type"),
    pytest.param("valid", [], "invalid_task_acceptance_criteria", id="empty-criteria"),
    pytest.param("valid", ["criterion"] * 11, "invalid_task_acceptance_criteria", id="too-many-criteria"),
    pytest.param("valid", "criterion", "invalid_task_acceptance_criteria", id="criteria-type"),
    pytest.param("valid", [""], "invalid_task_acceptance_criterion", id="empty-criterion"),
    pytest.param("valid", ["   "], "invalid_task_acceptance_criterion", id="blank-criterion"),
    pytest.param("valid", [" " + "c" * 301 + " "], "invalid_task_acceptance_criterion", id="long-criterion"),
    pytest.param("valid", [7], "invalid_task_acceptance_criterion", id="criterion-type"),
])
def test_direct_task_creation_rejects_invalid_input_without_writing(task_service, goal, criteria, code, keyed):
    service, workspace_id = task_service
    with pytest.raises(ValueError, match=f"^{code}$"):
        asyncio.run(service.create_task(workspace_id, goal, criteria, uuid4() if keyed else None))

    service.workspaces.get.assert_not_awaited()
    service.tasks.add.assert_not_awaited()
    service.tasks.add_with_idempotency_key.assert_not_awaited()
    service.session.commit.assert_not_awaited()
    service.session.rollback.assert_awaited_once()


@pytest.mark.parametrize("keyed", [False, True], ids=["without-key", "with-key"])
def test_direct_task_creation_normalizes_boundary_lengths(task_service, keyed):
    service, workspace_id = task_service
    goal = "g" * 500
    criterion = "c" * 300
    criteria = [f" {criterion}\u00a0"] + [" other "] * 9
    key = uuid4() if keyed else None

    task = asyncio.run(service.create_task(workspace_id, f"\t{goal}\n", criteria, key))

    assert task.goal == goal
    assert task.acceptance_criteria == (criterion, *("other",) * 9)
    assert criteria[0] == f" {criterion}\u00a0"
    if keyed:
        service.tasks.add_with_idempotency_key.assert_awaited_once_with(task, key)
        service.tasks.add.assert_not_awaited()
    else:
        service.tasks.add.assert_awaited_once_with(task)
        service.tasks.add_with_idempotency_key.assert_not_awaited()
    service.session.commit.assert_awaited_once()
