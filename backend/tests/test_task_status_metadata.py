import re

from sqlalchemy import CheckConstraint, String

from app.db.base import Base
from app.db.models import TaskModel
from app.domain.execution import AgentRunStatus
from app.domain.task import TaskStatus


def test_task_status_is_a_distinct_domain_enum():
    assert TaskStatus is not AgentRunStatus
    assert {status.value for status in TaskStatus} == {
        "queued", "running", "verifying", "succeeded", "failed", "cancelled",
    }
    assert all(type(status) is TaskStatus for status in TaskStatus)


def test_task_status_column_metadata():
    table = TaskModel.__table__
    assert Base.metadata.tables["tasks"] is table
    column = table.c.status
    assert isinstance(column.type, String)
    assert column.type.length == 32
    assert not column.nullable
    assert column.server_default is not None
    assert str(column.server_default.arg) == TaskStatus.QUEUED.value


def test_task_status_check_matches_domain_enum():
    checks = [constraint for constraint in TaskModel.__table__.constraints
              if isinstance(constraint, CheckConstraint) and constraint.name == "ck_tasks_status"]
    assert len(checks) == 1
    expression = str(checks[0].sqltext)
    assert expression.startswith("status IN (") and expression.endswith(")")
    assert set(re.findall(r"'([^']+)'", expression)) == {status.value for status in TaskStatus}
