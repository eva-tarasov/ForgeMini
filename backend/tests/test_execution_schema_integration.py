"""Opt-in checks on a separately migrated Task 05 database; all writes roll back."""

import asyncio
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import DateTime, delete, inspect, insert, null, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
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
                    assert await conn.scalar(text("SELECT current_database()")) == DB

                    def inspect_schema(sync_conn):
                        inspector = inspect(sync_conn)
                        names = {"agent_runs", "agent_events"}
                        tables = set(inspector.get_table_names())
                        assert names <= tables
                        assert not {"tool_calls", "approvals", "artifacts"} & tables      # dropped in C5 (ADR-003)
                        parents = {"agent_runs": ("task_id", "tasks"), "agent_events": ("agent_run_id", "agent_runs")}
                        nullable_columns = {"agent_runs": {"final_result"}}
                        for table, (column, target) in parents.items():
                            cols = inspector.get_columns(table)
                            assert {c["name"] for c in cols if c["nullable"]} == nullable_columns.get(table, set())
                            timestamps = [c for c in cols if c["name"] == "created_at"]
                            assert all(isinstance(c["type"], DateTime) and c["type"].timezone for c in timestamps)
                            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
                            fk = inspector.get_foreign_keys(table)[0]
                            assert fk["constrained_columns"] == [column] and fk["referred_table"] == target
                            assert fk["options"]["ondelete"] == "RESTRICT"
                            assert any(i["column_names"] == [column] for i in inspector.get_indexes(table))
                        assert {tuple(u["column_names"]) for u in inspector.get_unique_constraints("agent_runs")} >= {("task_id", "attempt_number")}
                        assert {tuple(u["column_names"]) for u in inspector.get_unique_constraints("agent_events")} >= {("agent_run_id", "sequence")}
                        for table in names:
                            assert inspector.get_check_constraints(table)
                        payload_column = next(c for c in inspector.get_columns("agent_events") if c["name"] == "payload")
                        assert isinstance(payload_column["type"], JSONB)
                        final_result = next(c for c in inspector.get_columns("agent_runs") if c["name"] == "final_result")
                        assert isinstance(final_result["type"], JSONB)
                        assert "ck_agent_runs_final_result_object" in {
                            c["name"] for c in inspector.get_check_constraints("agent_runs")
                        }

                    await conn.run_sync(inspect_schema)
                    workspace, task, run = uuid4(), uuid4(), uuid4()
                    await conn.execute(insert(WorkspaceModel).values(id=workspace, name="synthetic"))
                    await conn.execute(insert(TaskModel).values(id=task, workspace_id=workspace, goal="synthetic", acceptance_criteria=["ok"]))
                    await conn.execute(insert(AgentRunModel).values(
                        id=run, task_id=task, attempt_number=1, status="queued", final_result=null()
                    ))
                    assert await conn.scalar(
                        select(AgentRunModel.final_result.is_(None)).where(AgentRunModel.id == run)
                    ) is True
                    await conn.execute(insert(AgentRunModel).values(id=uuid4(), task_id=task, attempt_number=2, status="running"))
                    event = uuid4()
                    payload = {"synthetic": [1, "example"]}
                    await conn.execute(insert(AgentEventModel).values(id=event, agent_run_id=run, sequence=1, event_type="created", payload=payload))
                    row = (await conn.execute(select(AgentEventModel.payload, AgentEventModel.created_at).where(AgentEventModel.id == event))).one()
                    assert row.payload == payload and row.created_at.utcoffset() is not None
                    for model, timestamp, key in [(AgentRunModel, AgentRunModel.created_at, run)]:
                        value = await conn.scalar(select(timestamp).where(model.id == key))
                        assert value.utcoffset() is not None

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
                    for value in ("invalid", "waiting_approval"):                       # the M1 status is gone (C5)
                        await rejected(insert(AgentRunModel).values(**run_values(status=value)), "23514")
                    for value in ([], "synthetic", JSONB.NULL):
                        await rejected(insert(AgentRunModel).values(**run_values(final_result=value)), "23514")
                    await rejected(insert(AgentRunModel).values(**run_values(status=None)), "23502")
                    await rejected(insert(AgentEventModel).values(**event_values(payload=null())), "23502")
                    await rejected(insert(AgentEventModel).values(**event_values(event_type=" \t")), "23514")
                    await rejected(insert(AgentRunModel).values(**run_values(task_id=uuid4())), "23503")
                    await rejected(insert(AgentEventModel).values(**event_values(agent_run_id=uuid4())), "23503")
                    await rejected(delete(TaskModel).where(TaskModel.id == task), "23503")
                    await rejected(delete(AgentRunModel).where(AgentRunModel.id == run), "23503")
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(check())
