import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.api.tasks import create_task as create_task_endpoint
from app.domain.task import Task, TaskStatus
from app.repositories.tasks import TaskRepository
from app.schemas.task import TaskCreate
from app.services.tasks import IdempotencyKeyConflictError, IdempotencyRecordNotFoundError, TaskService


def service_with_mock_repositories(inserted=True, existing=None):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    tasks = SimpleNamespace(add=AsyncMock(), add_with_idempotency_key=AsyncMock(return_value=inserted),
                            get_by_idempotency_key=AsyncMock(return_value=existing))
    workspaces = SimpleNamespace(get=AsyncMock(return_value=object()))
    return TaskService(session, tasks, workspaces), session, tasks


def test_creation_without_key_produces_independent_tasks():
    service, session, tasks = service_with_mock_repositories()
    workspace = uuid4()
    first = asyncio.run(service.create_task(workspace, "goal", ["criterion"]))
    second = asyncio.run(service.create_task(workspace, "goal", ["criterion"]))
    assert first.id != second.id
    assert tasks.add.await_count == 2 and session.commit.await_count == 2
    tasks.add_with_idempotency_key.assert_not_awaited()


def test_first_keyed_insert_commits():
    service, session, tasks = service_with_mock_repositories()
    workspace, key = uuid4(), uuid4()
    task = asyncio.run(service.create_task(workspace, " goal ", [" criterion "], key))
    assert task.goal == "goal" and task.acceptance_criteria == ("criterion",)
    tasks.add_with_idempotency_key.assert_awaited_once_with(task, key)
    tasks.get_by_idempotency_key.assert_not_awaited()
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


def test_replay_preserves_identity_and_status():
    workspace, key = uuid4(), uuid4()
    existing = Task(uuid4(), workspace, "goal", ("criterion",), TaskStatus.VERIFYING)
    service, session, tasks = service_with_mock_repositories(False, existing)
    result = asyncio.run(service.create_task(workspace, " goal ", [" criterion "], key))
    assert result is existing and result.status is TaskStatus.VERIFYING
    tasks.get_by_idempotency_key.assert_awaited_once_with(workspace, key)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"goal": "other"}, {"acceptance_criteria": ("other",)}])
def test_different_payload_rejects_and_rolls_back(changes):
    workspace = uuid4()
    existing = replace(Task(uuid4(), workspace, "goal", ("criterion",)), **changes)
    service, session, _ = service_with_mock_repositories(False, existing)
    with pytest.raises(IdempotencyKeyConflictError):
        asyncio.run(service.create_task(workspace, "goal", ["criterion"], uuid4()))
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_missing_conflict_record_is_an_error():
    service, session, _ = service_with_mock_repositories(False)
    with pytest.raises(IdempotencyRecordNotFoundError):
        asyncio.run(service.create_task(uuid4(), "goal", ["criterion"], uuid4()))
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_unexpected_database_error_rolls_back():
    service, session, tasks = service_with_mock_repositories()
    tasks.add_with_idempotency_key.side_effect = RuntimeError("database failure")
    with pytest.raises(RuntimeError, match="database failure"):
        asyncio.run(service.create_task(uuid4(), "goal", ["criterion"], uuid4()))
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_cancelled_insert_rolls_back_and_propagates_through_endpoint():
    service, session, tasks = service_with_mock_repositories()
    cancellation = asyncio.CancelledError("cancelled during insert")
    tasks.add_with_idempotency_key.side_effect = cancellation
    payload = TaskCreate(workspace_id=uuid4(), goal="goal", acceptance_criteria=["criterion"])
    with pytest.raises(asyncio.CancelledError) as exc:
        asyncio.run(create_task_endpoint(payload, service, uuid4()))
    assert exc.value is cancellation
    tasks.add_with_idempotency_key.assert_awaited_once()
    tasks.get_by_idempotency_key.assert_not_awaited()
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("returned", [None, uuid4()])
def test_repository_insert_sql_and_transaction_ownership(returned):
    session = SimpleNamespace(scalar=AsyncMock(return_value=returned), commit=AsyncMock(), rollback=AsyncMock())
    task = Task(uuid4(), uuid4(), "goal", ("criterion",))
    key = uuid4()
    assert asyncio.run(TaskRepository(session).add_with_idempotency_key(task, key)) is (returned is not None)
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ON CONFLICT ON CONSTRAINT uq_tasks_workspace_idempotency_key DO NOTHING" in sql
    assert "RETURNING tasks.id" in sql and "DO UPDATE" not in sql
    assert key in compiled.params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_repository_separate_lookup_maps_status():
    workspace, key, task_id = uuid4(), uuid4(), uuid4()
    model = SimpleNamespace(id=task_id, workspace_id=workspace, goal="goal", acceptance_criteria=["criterion"],
                            status="running")
    session = SimpleNamespace(scalar=AsyncMock(return_value=model), commit=AsyncMock(), rollback=AsyncMock())
    task = asyncio.run(TaskRepository(session).get_by_idempotency_key(workspace, key))
    assert task.id == task_id and task.status is TaskStatus.RUNNING
    compiled = session.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    assert "tasks.workspace_id" in str(compiled) and "tasks.idempotency_key" in str(compiled)
    assert workspace in compiled.params.values() and key in compiled.params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
