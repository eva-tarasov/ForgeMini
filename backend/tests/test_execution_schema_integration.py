"""Opt-in checks on a separately migrated Task 05 database; all writes roll back."""

import asyncio
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import DateTime, delete, inspect, insert, null, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.core.settings import Settings
from app.db.models import (AgentEventModel, AgentRunModel, ApprovalModel, ArtifactModel,
                           TaskModel, ToolCallModel, WorkspaceModel)
from app.db.session import create_engine


DB = os.getenv("FORGE_TASK05_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="set FORGE_TASK05_TEST_DB to opt in")


def test_execution_schema_and_constraints():
    if not DB or not re.fullmatch(r"forge_task05_[a-z0-9_]+", DB) or os.getenv("POSTGRES_DB") != DB:
        pytest.fail("POSTGRES_DB and FORGE_TASK05_TEST_DB must name the same forge_task05_* database", pytrace=False)

    async def check():
        engine = create_engine(Settings(postgres_db=DB))
        try:
            async with engine.connect() as conn:
                transaction = await conn.begin()
                try:
                    def inspect_schema(sync_conn):
                        inspector = inspect(sync_conn)
                        names = {"agent_runs", "agent_events", "tool_calls", "approvals", "artifacts"}
                        assert names <= set(inspector.get_table_names())
                        parents = {"agent_runs": ("task_id", "tasks"), "agent_events": ("agent_run_id", "agent_runs"),
                                   "tool_calls": ("agent_run_id", "agent_runs"), "approvals": ("tool_call_id", "tool_calls"),
                                   "artifacts": ("agent_run_id", "agent_runs")}
                        for table, (column, target) in parents.items():
                            cols = inspector.get_columns(table)
                            assert all(not c["nullable"] for c in cols if c["name"] != "decided_at")
                            timestamps = [c for c in cols if c["name"] in ("created_at", "requested_at", "decided_at")]
                            assert all(isinstance(c["type"], DateTime) and c["type"].timezone for c in timestamps)
                            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
                            fk = inspector.get_foreign_keys(table)[0]
                            assert fk["constrained_columns"] == [column] and fk["referred_table"] == target
                            assert fk["options"]["ondelete"] == "RESTRICT"
                            assert any(i["column_names"] == [column] for i in inspector.get_indexes(table))
                        assert {tuple(u["column_names"]) for u in inspector.get_unique_constraints("agent_runs")} >= {("task_id", "attempt_number")}
                        assert {tuple(u["column_names"]) for u in inspector.get_unique_constraints("agent_events")} >= {("agent_run_id", "sequence")}
                        assert not any(u["column_names"] == ["tool_call_id"] for u in inspector.get_unique_constraints("approvals"))
                        for table in names:
                            assert inspector.get_check_constraints(table)
                        payload_column = next(c for c in inspector.get_columns("agent_events") if c["name"] == "payload")
                        assert isinstance(payload_column["type"], JSONB)

                    await conn.run_sync(inspect_schema)
                    workspace, task, run, tool = uuid4(), uuid4(), uuid4(), uuid4()
                    await conn.execute(insert(WorkspaceModel).values(id=workspace, name="synthetic"))
                    await conn.execute(insert(TaskModel).values(id=task, workspace_id=workspace, goal="synthetic", acceptance_criteria=["ok"]))
                    await conn.execute(insert(AgentRunModel).values(id=run, task_id=task, attempt_number=1, status="queued"))
                    await conn.execute(insert(AgentRunModel).values(id=uuid4(), task_id=task, attempt_number=2, status="running"))
                    event = uuid4()
                    payload = {"synthetic": [1, "example"]}
                    await conn.execute(insert(AgentEventModel).values(id=event, agent_run_id=run, sequence=1, event_type="created", payload=payload))
                    await conn.execute(insert(ToolCallModel).values(id=tool, agent_run_id=run, tool_name="synthetic", status="proposed"))
                    await conn.execute(insert(ApprovalModel), [{"id": uuid4(), "tool_call_id": tool, "status": "pending"}, {"id": uuid4(), "tool_call_id": tool, "status": "approved"}])
                    artifact = uuid4()
                    await conn.execute(insert(ArtifactModel).values(id=artifact, agent_run_id=run, kind="report", storage_ref="synthetic://result"))
                    row = (await conn.execute(select(AgentEventModel.payload, AgentEventModel.created_at).where(AgentEventModel.id == event))).one()
                    assert row.payload == payload and row.created_at.utcoffset() is not None
                    for model, timestamp, key in [(AgentRunModel, AgentRunModel.created_at, run), (ToolCallModel, ToolCallModel.created_at, tool), (ArtifactModel, ArtifactModel.created_at, artifact)]:
                        value = await conn.scalar(select(timestamp).where(model.id == key))
                        assert value.utcoffset() is not None
                    approval_time = await conn.scalar(select(ApprovalModel.requested_at).where(ApprovalModel.tool_call_id == tool).limit(1))
                    assert approval_time.utcoffset() is not None

                    async def rejected(statement, state):
                        with pytest.raises(IntegrityError) as exc:
                            async with conn.begin_nested():
                                await conn.execute(statement)
                        assert exc.value.orig.sqlstate == state

                    def run_values(**overrides):
                        return {"id": uuid4(), "task_id": task, "attempt_number": 3, "status": "queued", **overrides}
                    def event_values(**overrides):
                        return {"id": uuid4(), "agent_run_id": run, "sequence": 2, "event_type": "event", "payload": {}, **overrides}
                    await rejected(insert(AgentRunModel).values(**run_values(attempt_number=1)), "23505")
                    await rejected(insert(AgentEventModel).values(**event_values(sequence=1)), "23505")
                    for value in (0, -1):
                        await rejected(insert(AgentRunModel).values(**run_values(attempt_number=value)), "23514")
                        await rejected(insert(AgentEventModel).values(**event_values(sequence=value)), "23514")
                    await rejected(insert(AgentRunModel).values(**run_values(status="invalid")), "23514")
                    await rejected(insert(ToolCallModel).values(id=uuid4(), agent_run_id=run, tool_name="x", status="invalid"), "23514")
                    await rejected(insert(ApprovalModel).values(id=uuid4(), tool_call_id=tool, status="invalid"), "23514")
                    await rejected(insert(AgentRunModel).values(**run_values(status=None)), "23502")
                    await rejected(insert(AgentEventModel).values(**event_values(payload=null())), "23502")
                    await rejected(insert(ToolCallModel).values(id=uuid4(), agent_run_id=run, tool_name=None, status="proposed"), "23502")
                    await rejected(insert(AgentEventModel).values(**event_values(event_type=" \t")), "23514")
                    await rejected(insert(ToolCallModel).values(id=uuid4(), agent_run_id=run, tool_name=" ", status="proposed"), "23514")
                    for kind, ref in [(" ", "ok"), ("ok", " ")]:
                        await rejected(insert(ArtifactModel).values(id=uuid4(), agent_run_id=run, kind=kind, storage_ref=ref), "23514")
                    await rejected(insert(AgentRunModel).values(**run_values(task_id=uuid4())), "23503")
                    await rejected(insert(AgentEventModel).values(**event_values(agent_run_id=uuid4())), "23503")
                    await rejected(insert(ToolCallModel).values(id=uuid4(), agent_run_id=uuid4(), tool_name="x", status="proposed"), "23503")
                    await rejected(insert(ApprovalModel).values(id=uuid4(), tool_call_id=uuid4(), status="pending"), "23503")
                    await rejected(insert(ArtifactModel).values(id=uuid4(), agent_run_id=uuid4(), kind="x", storage_ref="x"), "23503")
                    await rejected(delete(TaskModel).where(TaskModel.id == task), "23503")
                    await rejected(delete(AgentRunModel).where(AgentRunModel.id == run), "23503")
                    await rejected(delete(ToolCallModel).where(ToolCallModel.id == tool), "23503")
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(check())
