from uuid import uuid4

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY

from app.db.base import Base
from app.db.models import TaskModel, WorkspaceModel
from app.domain.task import Task, TaskStatus


def test_workspace_metadata():
    table = WorkspaceModel.__table__
    assert Base.metadata.tables["workspaces"] is table
    assert set(table.columns.keys()) == {"id", "name"}
    assert isinstance(table.c.id.type, Uuid)
    assert table.c.id.primary_key
    assert isinstance(table.c.name.type, String)
    assert table.c.name.type.length == 100
    assert not table.c.name.nullable
    assert {c.name for c in table.constraints if isinstance(c, CheckConstraint)} == {
        "ck_workspaces_name_not_blank"
    }


def test_task_metadata():
    table = TaskModel.__table__
    assert Base.metadata.tables["tasks"] is table
    assert set(table.columns.keys()) == {
        "id", "workspace_id", "goal", "acceptance_criteria", "status", "idempotency_key"
    }
    assert isinstance(table.c.id.type, Uuid)
    assert table.c.id.primary_key
    assert isinstance(table.c.workspace_id.type, Uuid)
    assert not table.c.workspace_id.nullable
    fk = next(iter(table.c.workspace_id.foreign_keys))
    assert fk.target_fullname == "workspaces.id"
    assert fk.ondelete == "RESTRICT"
    assert table.c.goal.type.length == 500
    assert not table.c.goal.nullable
    assert isinstance(table.c.acceptance_criteria.type, ARRAY)
    assert isinstance(table.c.acceptance_criteria.type.item_type, Text)
    assert not table.c.acceptance_criteria.nullable
    assert {c.name for c in table.constraints if isinstance(c, CheckConstraint)} == {
        "ck_tasks_goal_not_blank", "ck_tasks_acceptance_criteria_count", "ck_tasks_status"
    }
    assert any([column.name for column in index.columns] == ["workspace_id"] for index in table.indexes)


def test_task_idempotency_key_metadata():
    table = TaskModel.__table__
    column = table.c.idempotency_key
    assert isinstance(column.type, Uuid)
    assert column.nullable and not column.primary_key
    assert column.default is None and column.server_default is None
    constraints = [c for c in table.constraints if isinstance(c, UniqueConstraint)
                   and c.name == "uq_tasks_workspace_idempotency_key"]
    assert len(constraints) == 1
    assert [c.name for c in constraints[0].columns] == ["workspace_id", "idempotency_key"]


def test_task_domain_construction_remains_unchanged():
    task_id, workspace_id = uuid4(), uuid4()
    task = Task(task_id, workspace_id, "goal", ("criterion",))
    assert (task.id, task.workspace_id, task.goal, task.acceptance_criteria, task.status) == (
        task_id, workspace_id, "goal", ("criterion",), TaskStatus.QUEUED
    )
