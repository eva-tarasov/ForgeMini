"""Whole pipeline on REAL PostgreSQL + REAL Docker, scripted model (no LLM):

    create task/run -> AgentService.execute_run(SandboxRunner) -> SandboxVerifier -> terminal status

These tests are the product's honesty contract: `succeeded` only with fresh-container evidence.
"""
import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.workspace import Workspace
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.docker_sandbox import default_image, docker_available
from app.sandbox.runner import SandboxJob, SandboxRunner
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError
from app.services.agent_service import AgentService
from app.services.sandbox_verifier import SandboxVerifier
from app.services.tasks import TaskService
from sandbox_support import ScriptedModel, tool_turn

pytestmark = [
    pytest.mark.skipif(os.getenv("FORGE_TASK06_TEST_DB") is None,
                       reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in"),
    pytest.mark.skipif(not asyncio.run(docker_available()), reason="no Docker daemon available"),
]

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-calc"
AVG, DIV = "python -m unittest tests.test_average", "python -m unittest tests.test_divide"

FIX_AVERAGE = [
    tool_turn("bash", {"command": AVG}),
    tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "sum(numbers) // len(numbers)",
                            "new_str": "sum(numbers) / len(numbers)"}),
    tool_turn("bash", {"command": AVG}),
    tool_turn("finish", {"status": "done", "summary": "average used floor division"}),
]


@asynccontextmanager
async def world(tmp_path: Path, test_command: str):
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    engine = create_engine(Settings(postgres_db=name))
    factory = create_session_factory(engine)
    project = tmp_path / "project"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("__pycache__"))
    workspace_id = uuid4()
    ids: dict = {}
    try:
        async with factory() as session:
            repo = WorkspaceRepository(session)
            await repo.add(Workspace(workspace_id, "pipeline", str(project), test_command, default_image()))
            await session.flush()
            task = await TaskService(session, TaskRepository(session), repo).create_task(
                workspace_id, "fix the bug", ["tests pass"])
            ids["task_id"] = task.id
        async with factory() as session:
            run, _ = await AgentRunService(session).create_initial_run(ids["task_id"])
            ids["run_id"] = run.id
        yield factory, project, FileArtifactStore(tmp_path / "artifacts"), ids["run_id"], ids["task_id"]
    finally:
        async with factory.begin() as session:
            if "run_id" in ids:
                await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == ids["run_id"]))
                await session.execute(delete(AgentRunModel).where(AgentRunModel.id == ids["run_id"]))
            await session.execute(delete(TaskModel).where(TaskModel.workspace_id == workspace_id))
            await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
        await engine.dispose()


async def execute(factory, project, store, run_id, turns, test_command):
    async def resolve(_req):
        return SandboxJob(project, default_image(), test_command)
    runner = SandboxRunner(model=ScriptedModel(turns), resolve_job=resolve, artifacts=store)
    await AgentService(factory, runner, execution_timeout_seconds=300).execute_run(run_id)


async def state(factory, run_id: UUID, task_id: UUID):
    async with factory() as s:
        run = await s.get(AgentRunModel, run_id)
        task = await s.get(TaskModel, task_id)
        events = (await s.scalars(select(AgentEventModel).where(AgentEventModel.agent_run_id == run_id)
                                  .order_by(AgentEventModel.sequence))).all()
        return run.status, task.status, events


def scenario(test_command, turns, *, before_verify=None):
    async def body(tmp_path):
        async with world(tmp_path, test_command) as (factory, project, store, run_id, task_id):
            await execute(factory, project, store, run_id, turns, test_command)
            mid_status, _, _ = await state(factory, run_id, task_id)
            if before_verify:
                before_verify(project)
            verification = None
            if mid_status == "verifying":
                verification = await SandboxVerifier(factory, store).verify(run_id)
            run_status, task_status, events = await state(factory, run_id, task_id)
            return mid_status, verification, run_status, task_status, events
    return body


def go(tmp_path, test_command, turns, **kw):
    return asyncio.run(asyncio.wait_for(scenario(test_command, turns, **kw)(tmp_path), 600))


def test_correct_fix_is_verified_in_fresh_containers_and_succeeds(tmp_path):
    mid, v, run_status, task_status, events = go(tmp_path, AVG, FIX_AVERAGE)
    assert mid == "verifying"                                         # runner alone never reaches success
    assert v.reason == "tests_fail_before_and_pass_after"
    assert v.baseline.exit_code != 0 and v.patched.exit_code == 0     # red before, green after
    assert (run_status, task_status) == ("succeeded", "succeeded")
    kinds = [e.event_type for e in events]
    i = kinds.index("sandbox_verification_recorded")
    # evidence is committed together with the status change, directly before it
    assert events[i + 1].event_type == "agent_run_status_changed" and events[i + 1].payload["to"] == "succeeded"
    assert events[i].payload["changed_paths"] == ["calc/ops.py"]


def test_agent_that_weakens_the_test_instead_of_fixing_the_code_cannot_succeed(tmp_path):
    """The real attack: make the red test green by changing its EXPECTATION, not the code.

    Without the protected-files rule this run would be a textbook false PASS: the test is red on the
    pristine source (baseline) and green with the agent's change.
    """
    turns = [tool_turn("edit_file", {"path": "tests/test_average.py",
                                     "old_str": "self.assertEqual(average([1, 2]), 1.5)",
                                     "new_str": "self.assertEqual(average([1, 2]), 1)"}),   # matches the bug
             tool_turn("bash", {"command": AVG}),
             tool_turn("finish", {"status": "done", "summary": "tests are green now"})]
    mid, v, run_status, task_status, _ = go(tmp_path, AVG, turns)
    assert mid == "verifying" and v.patched is None                   # rejected before wasting a container
    assert v.reason == "protected_files_modified: tests/test_average.py"
    assert (run_status, task_status) == ("failed", "failed")


def test_honest_cannot_fix_ends_failed_with_reason_and_never_reaches_verification(tmp_path):
    turns = [tool_turn("finish", {"status": "cannot_fix", "summary": "contradicts the documented test"})]
    mid, v, run_status, _, events = go(tmp_path, DIV, turns)
    assert mid == "failed" and v is None and run_status == "failed"
    assert not any(e.event_type == "sandbox_verification_recorded" for e in events)


def test_done_without_changes_is_failed_not_succeeded(tmp_path):
    mid, v, run_status, _, _ = go(tmp_path, AVG, [tool_turn("finish", {"status": "done", "summary": "fine"})])
    assert (mid, v, run_status) == ("failed", None, "failed")


def test_tests_that_were_already_green_do_not_prove_a_fix(tmp_path):
    """Agent edits an unrelated file for a test that passes anyway: honest answer is 'cannot prove it'."""
    turns = [tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "return a / b", "new_str": "return (a / b)"}),
             tool_turn("finish", {"status": "done", "summary": "tweaked divide"})]
    mid, v, run_status, task_status, _ = go(tmp_path, DIV, turns)
    assert v.verdict.value == "inconclusive" and v.reason.startswith("baseline_already_green")
    assert (run_status, task_status) == ("failed", "failed")           # never reported as success


def test_agent_claiming_success_while_tests_still_fail_is_failed(tmp_path):
    turns = [tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "// len", "new_str": "// (len"}),   # junk edit
             tool_turn("finish", {"status": "done", "summary": "should be fixed now"})]
    # the junk edit is a syntax error; tests cannot pass
    mid, v, run_status, _, _ = go(tmp_path, AVG, turns)
    assert v.reason.startswith("tests_failed") and run_status == "failed"


def test_source_modified_after_the_run_makes_verification_inconclusive(tmp_path):
    def tamper(project: Path):
        (project / "calc" / "ops.py").write_text((project / "calc" / "ops.py").read_text() + "\n# edited\n")
    mid, v, run_status, _, _ = go(tmp_path, AVG, FIX_AVERAGE, before_verify=tamper)
    assert v.reason.startswith("source_changed_since_run") and run_status == "failed"


def test_verifying_twice_is_rejected_after_the_terminal_state(tmp_path):
    async def body():
        async with world(tmp_path, AVG) as (factory, project, store, run_id, task_id):
            await execute(factory, project, store, run_id, FIX_AVERAGE, AVG)
            verifier = SandboxVerifier(factory, store)
            await verifier.verify(run_id)
            with pytest.raises(StaleAgentRunStatusError):
                await verifier.verify(run_id)
    asyncio.run(asyncio.wait_for(body(), 600))
