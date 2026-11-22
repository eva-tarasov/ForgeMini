"""Opt-in checks against a separately created and migrated test database."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import String, Text, Uuid, delete, inspect, insert, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.settings import Settings
from app.db.models import TaskModel, WorkspaceModel
from app.db.session import create_engine


TEST_DATABASE = os.environ.get("FORGE_TEST_SCHEMA_DB")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE,
    reason="set FORGE_TEST_SCHEMA_DB to a separate migrated test database",
)


def run_check(check):
    # Only an explicitly named test database may be used for write/rollback checks.
    if not TEST_DATABASE or not TEST_DATABASE.startswith("forge_task03_"):
        pytest.fail("FORGE_TEST_SCHEMA_DB must start with forge_task03_", pytrace=False)
    failure = None
    try:
        asyncio.run(check())
    except (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        failure = type(getattr(exc, "orig", exc)).__name__
    if failure:
        pytest.fail(f"Schema check failed ({failure})", pytrace=False)


def test_migrated_schema_metadata():
    async def check():
        engine = create_engine(Settings(postgres_db=TEST_DATABASE))
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT current_database()")) == TEST_DATABASE

                def inspect_schema(sync_connection):
                    inspector = inspect(sync_connection)
                    assert {"workspaces", "tasks", "alembic_version"} <= set(inspector.get_table_names())
                    workspaces = {c["name"]: c for c in inspector.get_columns("workspaces")}
                    tasks = {c["name"]: c for c in inspector.get_columns("tasks")}
                    assert set(workspaces) == {"id", "name", "source_path", "test_command", "image"}
                    assert set(tasks) == {"id", "workspace_id", "goal", "acceptance_criteria", "status", "idempotency_key"}
                    assert {n for n, c in workspaces.items() if c["nullable"]} == {"source_path", "test_command", "image"}  # run config is optional
                    assert {name for name, column in tasks.items() if column["nullable"]} == {"idempotency_key"}
                    assert isinstance(workspaces["id"]["type"], Uuid)
                    assert all(isinstance(tasks[name]["type"], Uuid) for name in ("id", "workspace_id", "idempotency_key"))
                    assert workspaces["name"]["type"].length == 100
                    assert isinstance(tasks["goal"]["type"], String) and tasks["goal"]["type"].length == 500
                    assert isinstance(tasks["acceptance_criteria"]["type"], ARRAY)
                    assert isinstance(tasks["acceptance_criteria"]["type"].item_type, Text)
                    assert isinstance(tasks["status"]["type"], String) and tasks["status"]["type"].length == 32
                    assert tasks["status"]["default"] == "'queued'::character varying"
                    assert inspector.get_pk_constraint("workspaces")["constrained_columns"] == ["id"]
                    assert inspector.get_pk_constraint("tasks")["constrained_columns"] == ["id"]
                    fk = inspector.get_foreign_keys("tasks")[0]
                    assert fk["constrained_columns"] == ["workspace_id"]
                    assert fk["referred_table"] == "workspaces"
                    assert fk["referred_columns"] == ["id"]
                    assert fk["options"]["ondelete"] == "RESTRICT"
                    assert {c["name"] for c in inspector.get_check_constraints("workspaces")} == {
                        "ck_workspaces_name_not_blank", "ck_workspaces_run_config_not_blank",
                    }
                    assert {c["name"] for c in inspector.get_check_constraints("tasks")} == {
                        "ck_tasks_goal_not_blank", "ck_tasks_acceptance_criteria_count", "ck_tasks_status"
                    }
                    assert {
                        (constraint["name"], tuple(constraint["column_names"]))
                        for constraint in inspector.get_unique_constraints("tasks")
                    } == {
                        ("uq_tasks_workspace_idempotency_key", ("workspace_id", "idempotency_key")),
                    }
                    assert any(
                        i["column_names"] == ["workspace_id"]
                        for i in inspector.get_indexes("tasks")
                    )

                await connection.run_sync(inspect_schema)
        finally:
            await engine.dispose()

    run_check(check)


def test_migrated_schema_constraints():
    async def check():
        engine = create_engine(Settings(postgres_db=TEST_DATABASE))
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    assert await connection.scalar(text("SELECT current_database()")) == TEST_DATABASE
                    workspace_id, task_id = uuid4(), uuid4()
                    await connection.execute(insert(WorkspaceModel).values(id=workspace_id, name="test"))
                    await connection.execute(insert(TaskModel).values(
                        id=task_id, workspace_id=workspace_id, goal="test", acceptance_criteria=["ok"]
                    ))
                    assert (await connection.execute(
                        select(TaskModel.status, TaskModel.idempotency_key).where(TaskModel.id == task_id)
                    )).one() == ("queued", None)
                    await connection.execute(insert(TaskModel).values(
                        id=uuid4(), workspace_id=workspace_id, goal="test", acceptance_criteria=["ok"] * 10
                    ))

                    async def rejected(statement, sqlstate):
                        with pytest.raises(DBAPIError) as exc_info:
                            async with connection.begin_nested():
                                await connection.execute(statement)
                        assert exc_info.value.orig.sqlstate == sqlstate

                    for name in ("", " \t\n"):
                        await rejected(insert(WorkspaceModel).values(id=uuid4(), name=name), "23514")
                    await rejected(insert(WorkspaceModel).values(id=uuid4(), name=None), "23502")
                    await rejected(insert(WorkspaceModel).values(id=uuid4(), name="x" * 101), "22001")
                    await rejected(insert(WorkspaceModel).values(id=workspace_id, name="duplicate"), "23505")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=workspace_id, goal="test", acceptance_criteria=["ok"], status="invalid"
                    ), "23514")
                    for goal in ("", " \t\n"):
                        await rejected(insert(TaskModel).values(
                            id=uuid4(), workspace_id=workspace_id, goal=goal, acceptance_criteria=["ok"]
                        ), "23514")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=workspace_id, goal="x" * 501, acceptance_criteria=["ok"]
                    ), "22001")
                    for criteria in ([], ["ok"] * 11):
                        await rejected(insert(TaskModel).values(
                            id=uuid4(), workspace_id=workspace_id, goal="test", acceptance_criteria=criteria
                        ), "23514")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=workspace_id, goal="test", acceptance_criteria=None
                    ), "23502")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=None, goal="test", acceptance_criteria=["ok"]
                    ), "23502")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=workspace_id, goal=None, acceptance_criteria=["ok"]
                    ), "23502")
                    await rejected(insert(TaskModel).values(
                        id=uuid4(), workspace_id=uuid4(), goal="test", acceptance_criteria=["ok"]
                    ), "23503")
                    await rejected(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id), "23503")
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    run_check(check)
