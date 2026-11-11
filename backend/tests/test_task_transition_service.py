import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.execution import AgentRunStatus
from app.domain.task import Task, TaskStatus as Status
from app.domain.task_transitions import InvalidTaskTransition
from app.repositories.tasks import TaskRepository
from app.services.tasks import (
    StaleTaskStatusError, TaskNotFoundError, TaskService, TaskVerificationEvidenceRequiredError,
)


def make_service(status="queued"):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    task = SimpleNamespace(status=status)
    repository = SimpleNamespace(get_for_update=AsyncMock(return_value=task))
    return TaskService(session, repository, MagicMock()), session, repository, task


def test_valid_transition_commits_once():
    service, session, repo, task = make_service()
    task_id = uuid4()
    async def commit():
        assert task.status == "running"
    session.commit.side_effect = commit
    asyncio.run(service.transition_status(task_id, Status.QUEUED, Status.RUNNING))
    repo.get_for_update.assert_awaited_once_with(task_id)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize("actual,expected,target,error", [
    (None, Status.QUEUED, Status.RUNNING, TaskNotFoundError),
    ("running", Status.QUEUED, Status.RUNNING, StaleTaskStatusError),
    ("queued", Status.QUEUED, Status.SUCCEEDED, InvalidTaskTransition),
    ("failed", Status.FAILED, Status.RUNNING, InvalidTaskTransition),
    ("verifying", Status.VERIFYING, Status.SUCCEEDED, TaskVerificationEvidenceRequiredError),
])
def test_rejections_rollback_without_mutation(actual, expected, target, error):
    service, session, repo, task = make_service(actual)
    if actual is None:
        repo.get_for_update.return_value = None
    with pytest.raises(error):
        asyncio.run(service.transition_status(uuid4(), expected, target))
    assert task.status == actual
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_old_expected_status_cannot_commit_twice():
    service, session, _, task = make_service()
    task_id = uuid4()
    asyncio.run(service.transition_status(task_id, Status.QUEUED, Status.RUNNING))
    with pytest.raises(StaleTaskStatusError) as exc:
        asyncio.run(service.transition_status(task_id, Status.QUEUED, Status.RUNNING))
    assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.RUNNING)
    assert task.status == "running"
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("failure", [RuntimeError("commit failed"), asyncio.CancelledError()])
def test_failed_commit_rolls_back(failure):
    service, session, _, _ = make_service()
    session.commit.side_effect = failure
    with pytest.raises(type(failure)):
        asyncio.run(service.transition_status(uuid4(), Status.QUEUED, Status.RUNNING))
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("task_id,expected,target", [
    ("not-a-uuid", Status.QUEUED, Status.RUNNING),
    (uuid4(), "queued", Status.RUNNING),
    (uuid4(), Status.QUEUED, AgentRunStatus.RUNNING),
])
def test_invalid_arguments_are_rejected_before_lock(task_id, expected, target):
    service, session, repo, _ = make_service()
    with pytest.raises(TypeError):
        asyncio.run(service.transition_status(task_id, expected, target))
    repo.get_for_update.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_repository_uses_lock_without_commit():
    session = SimpleNamespace(scalar=AsyncMock(return_value=None), commit=AsyncMock())
    task_id = uuid4()
    assert asyncio.run(TaskRepository(session).get_for_update(task_id)) is None
    statement = session.scalar.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql and "tasks.id =" in sql
    assert task_id in statement.compile(dialect=postgresql.dialect()).params.values()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("status", list(Status))
def test_repository_maps_status_on_read_and_write(status):
    task_id, workspace_id = uuid4(), uuid4()
    model = SimpleNamespace(id=task_id, workspace_id=workspace_id, goal="goal",
                            acceptance_criteria=["criterion"], status=status.value)
    session = SimpleNamespace(get=AsyncMock(return_value=model), add=MagicMock(), commit=AsyncMock())
    repository = TaskRepository(session)
    task = asyncio.run(repository.get(task_id))
    assert task.status is status and task.acceptance_criteria == ("criterion",)
    asyncio.run(repository.add(task))
    stored = session.add.call_args.args[0]
    assert stored.status == status.value and stored.id == task_id
    session.commit.assert_not_awaited()


def test_default_task_status_and_immutability():
    task = Task(uuid4(), uuid4(), "goal", ("criterion",))
    assert task.status is Status.QUEUED
    with pytest.raises(FrozenInstanceError):
        task.status = Status.RUNNING


def test_repository_missing_task():
    session = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())
    assert asyncio.run(TaskRepository(session).get(uuid4())) is None
    session.commit.assert_not_awaited()
