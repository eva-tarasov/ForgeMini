"""ADR-003 / C5 migration c5a1f0d2e7b9 on real PostgreSQL: drops the M1 tables and 'waiting_approval', and refuses
(fail-closed, fully rolled back) while any run or task is still paused in 'waiting_approval'.

Opt-in: FORGE_MIGRATION_TEST_DB names a dedicated forge_migration_* database, migrated to head by
scripts/ci/integration_tests.sh. The test walks it down and up again with the real `alembic` command; it leaves the
database at head and empty.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.settings import Settings

DB = os.getenv("FORGE_MIGRATION_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="set FORGE_MIGRATION_TEST_DB to opt in")
BACKEND = Path(__file__).resolve().parents[1]
BEFORE, C5 = "a7c3e91d4b20", "c5a1f0d2e7b9"
# Later migrations exist on top of C5: the test steps exactly through C5 and returns the DB to the real head.
HEAD = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
M1_TABLES = {"tool_calls", "approvals", "artifacts"}
IDS = ("00000000-0000-4000-8000-00000000000" + d for d in "123")


def _alembic(*args):
    env = {**os.environ, "POSTGRES_DB": DB}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=120)


def _sql(query, *params):
    async def run():
        s = Settings(postgres_db=DB)
        conn = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port, user=s.postgres_user,
                                     password=s.postgres_password.get_secret_value(), database=DB)
        try:
            return await conn.fetch(query, *params)
        finally:
            await conn.close()
    return asyncio.run(run())


def _state():
    revision = _sql("SELECT version_num FROM alembic_version")[0][0]
    tables = {r[0] for r in _sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    return revision, tables & M1_TABLES


def _stuck_rows():
    workspace, task, run = IDS
    _sql("INSERT INTO workspaces (id, name) VALUES ($1, 'synthetic')", workspace)
    _sql("INSERT INTO tasks (id, workspace_id, goal, acceptance_criteria, status) "
         "VALUES ($1, $2, 'synthetic', ARRAY['ok'], 'waiting_approval')", task, workspace)
    _sql("INSERT INTO agent_runs (id, task_id, attempt_number, status) VALUES ($1, $2, 1, 'waiting_approval')",
         run, task)
    return workspace, task, run


def test_upgrade_refuses_paused_m1_rows_then_drops_tables_and_round_trips():
    if not re.fullmatch(r"forge_migration_[a-z0-9_]+", DB or ""):
        pytest.fail("FORGE_MIGRATION_TEST_DB must name a dedicated forge_migration_* database", pytrace=False)
    assert _state() == (HEAD, set())                                   # head: M1 tables are gone

    assert _alembic("downgrade", BEFORE).returncode == 0
    assert _state() == (BEFORE, M1_TABLES)                              # downgrade restores them
    workspace, task, run = _stuck_rows()
    try:
        refused = _alembic("upgrade", C5)
        assert refused.returncode != 0
        assert "refusing to migrate" in refused.stderr and "'agent_runs': 1, 'tasks': 1" in refused.stderr
        assert _state() == (BEFORE, M1_TABLES)                          # transactional DDL: nothing half-applied
        assert {r[0] for r in _sql("SELECT status FROM tasks WHERE id = $1", task)} == {"waiting_approval"}

        _sql("UPDATE agent_runs SET status = 'cancelled' WHERE id = $1", run)
        _sql("UPDATE tasks SET status = 'cancelled' WHERE id = $1", task)
        assert _alembic("upgrade", C5).returncode == 0
        assert _state() == (C5, set())
        with pytest.raises(asyncpg.CheckViolationError):               # the status is now impossible
            _sql("UPDATE tasks SET status = 'waiting_approval' WHERE id = $1", task)
        with pytest.raises(asyncpg.CheckViolationError):
            _sql("UPDATE agent_runs SET status = 'waiting_approval' WHERE id = $1", run)
    finally:
        if _state()[0] != C5:
            _sql("DELETE FROM agent_runs WHERE id = $1", run)
            _sql("DELETE FROM tasks WHERE id = $1", task)
            _alembic("upgrade", C5)
        _sql("DELETE FROM agent_runs WHERE id = $1", run)
        _sql("DELETE FROM tasks WHERE id = $1", task)
        _sql("DELETE FROM workspaces WHERE id = $1", workspace)
        _alembic("upgrade", "head")
    assert _state() == (HEAD, set())
