from sqlalchemy import CheckConstraint, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY

from app.db.base import Base
from app.db.models import TaskModel, WorkspaceModel


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
    assert set(table.columns.keys()) == {"id", "workspace_id", "goal", "acceptance_criteria"}
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
        "ck_tasks_goal_not_blank", "ck_tasks_acceptance_criteria_count"
    }
    assert any([column.name for column in index.columns] == ["workspace_id"] for index in table.indexes)
