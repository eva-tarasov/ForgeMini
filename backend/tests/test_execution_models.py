import re

import pytest
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, ArtifactModel, ToolCallModel
from app.domain.execution import AgentRunStatus, ApprovalStatus, ToolCallStatus


@pytest.mark.parametrize(
    "model,parent_column,target,timestamp",
    [
        (AgentRunModel, "task_id", "tasks.id", "created_at"),
        (AgentEventModel, "agent_run_id", "agent_runs.id", "created_at"),
        (ToolCallModel, "agent_run_id", "agent_runs.id", "created_at"),
        (ApprovalModel, "tool_call_id", "tool_calls.id", "requested_at"),
        (ArtifactModel, "agent_run_id", "agent_runs.id", "created_at"),
    ],
)
def test_common_metadata(model, parent_column, target, timestamp):
    table = model.__table__
    assert Base.metadata.tables[table.name] is table
    assert isinstance(table.c.id.type, Uuid) and table.c.id.primary_key
    assert all(not c.nullable for c in table.c if c.name != "decided_at")
    parent = table.c[parent_column]
    assert isinstance(parent.type, Uuid)
    foreign_key = next(iter(parent.foreign_keys))
    assert foreign_key.target_fullname == target
    assert foreign_key.ondelete == "RESTRICT"
    assert any([c.name for c in index.columns] == [parent_column] for index in table.indexes)
    column = table.c[timestamp]
    assert isinstance(column.type, DateTime) and column.type.timezone
    assert column.server_default is not None


@pytest.mark.parametrize("model,enum", [(AgentRunModel, AgentRunStatus), (ToolCallModel, ToolCallStatus), (ApprovalModel, ApprovalStatus)])
def test_status_checks_match_domain_enum(model, enum):
    table = model.__table__
    assert isinstance(table.c.status.type, String)
    checks = [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]
    status_check = next(check for check in checks if check.startswith("status IN ("))
    assert set(re.findall(r"'([^']+)'", status_check)) == {item.value for item in enum}


def test_unique_and_other_checks():
    for model, columns in [(AgentRunModel, ["task_id", "attempt_number"]), (AgentEventModel, ["agent_run_id", "sequence"])]:
        assert any([c.name for c in constraint.columns] == columns for constraint in model.__table__.constraints if isinstance(constraint, UniqueConstraint))
    assert not any(isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["tool_call_id"] for c in ApprovalModel.__table__.constraints)
    for model, names in [
        (AgentRunModel, {"ck_agent_runs_attempt_positive", "ck_agent_runs_status"}),
        (AgentEventModel, {"ck_agent_events_sequence_positive", "ck_agent_events_type_not_blank"}),
        (ToolCallModel, {"ck_tool_calls_name_not_blank", "ck_tool_calls_status"}),
        (ApprovalModel, {"ck_approvals_status"}),
        (ArtifactModel, {"ck_artifacts_kind_not_blank", "ck_artifacts_storage_ref_not_blank"}),
    ]:
        assert {c.name for c in model.__table__.constraints if isinstance(c, CheckConstraint)} == names
    assert isinstance(AgentEventModel.__table__.c.payload.type, JSONB)
    assert ApprovalModel.__table__.c.decided_at.nullable
    assert ApprovalModel.__table__.c.decided_at.type.timezone
    run_checks = {str(c.sqltext) for c in AgentRunModel.__table__.constraints if isinstance(c, CheckConstraint)}
    event_checks = {str(c.sqltext) for c in AgentEventModel.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "attempt_number >= 1" in run_checks
    assert "sequence >= 1" in event_checks
    for model, field in [(AgentEventModel, "event_type"), (ToolCallModel, "tool_name"), (ArtifactModel, "kind"), (ArtifactModel, "storage_ref")]:
        checks = {str(c.sqltext) for c in model.__table__.constraints if isinstance(c, CheckConstraint)}
        assert f"{field} ~ '[^[:space:]]'" in checks
