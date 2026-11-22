"""Shared PostgreSQL harness for the run-lifecycle and run-read integration tests (ADR-003, C3).

`make_run_scenario()` returns `run(scenario)`: seed workspace + task + ONE queued run, `await scenario(case)`, always clean up.
`case` has: factory, workspace_id, task_id, run_id and `idle()`: assert that no other connection is left
"idle in transaction" (a read endpoint that leaks an open transaction would hold locks and bloat the database).
Opt-in: FORGE_TASK06_TEST_DB and POSTGRES_DB must name the same forge_task06_* database.
"""
import asyncio
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory


def make_run_scenario():
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)

    def run(scenario):
        async def main():
            engine = create_engine(Settings(postgres_db=name))
            factory = create_session_factory(engine)
            case = SimpleNamespace(factory=factory, workspace_id=uuid4(), task_id=uuid4(), run_id=uuid4())

            def idle():
                async def check():
                    async with factory() as session:
                        leaked = await session.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"))
                        assert leaked == 0, "a request left a connection idle in transaction"
                return check()

            case.idle = idle
            seeded = False
            try:
                async with factory.begin() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    session.add(WorkspaceModel(id=case.workspace_id, name="synthetic run scenario"))
                    await session.flush()
                    session.add(TaskModel(id=case.task_id, workspace_id=case.workspace_id, goal="Run fixture",
                                          acceptance_criteria=["Reach a terminal status"], status="queued"))
                    await session.flush()
                    session.add(AgentRunModel(id=case.run_id, task_id=case.task_id, attempt_number=1, status="queued"))
                    seeded = True
                await asyncio.wait_for(scenario(case), timeout=30)
            finally:
                try:
                    if seeded:
                        async with factory.begin() as session:
                            runs = select(AgentRunModel.id).where(AgentRunModel.task_id == case.task_id)
                            await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id.in_(runs)))
                            await session.execute(delete(AgentRunModel).where(AgentRunModel.task_id == case.task_id))
                            await session.execute(delete(TaskModel).where(TaskModel.id == case.task_id))
                            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == case.workspace_id))
                finally:
                    await asyncio.wait_for(engine.dispose(), timeout=10)

        asyncio.run(main())

    return run
