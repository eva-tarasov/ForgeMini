"""ForgeMini through its PUBLIC HTTP API only (what the CLI and a future app do), real PostgreSQL + Docker.

The model is scripted, everything else is the real product: composition root, coordinator, sandbox runner,
verifier, evidence, Task sync, read API.
"""
import asyncio
import os
import re
import shutil
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.bootstrap import ForgeMiniSettings, create_forge_app
from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.sandbox.docker_sandbox import default_image, docker_available
from sandbox_support import ScriptedModel, tool_turn

pytestmark = [
    pytest.mark.skipif(os.getenv("FORGE_TASK06_TEST_DB") is None,
                       reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in"),
    pytest.mark.skipif(not asyncio.run(docker_available()), reason="no Docker daemon available"),
]

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-calc"
AVG = "python -m unittest tests.test_average"
DIV = "python -m unittest tests.test_divide"
FIX_AVERAGE = [
    tool_turn("bash", {"command": AVG}),
    tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "sum(numbers) // len(numbers)",
                            "new_str": "sum(numbers) / len(numbers)"}),
    tool_turn("bash", {"command": AVG}),
    tool_turn("finish", {"status": "done", "summary": "average used floor division"}),
]


def _db_settings() -> Settings:
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    return Settings(postgres_db=name)


@pytest.fixture
def api(tmp_path):
    """Yields (client, make_run) and cleans every row it created."""
    project = tmp_path / "project"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("__pycache__"))
    created = {"workspaces": []}
    holder = {"turns": []}

    def build(turns, *, key_present=True, **settings):
        holder["turns"] = turns

        def factory():
            if not key_present:
                from app.services.in_process_run_coordinator import RunnerConfigurationError
                raise RunnerConfigurationError("OPENAI_API_KEY and FORGE_MODEL_NAME must be set in the server environment (.env)")
            return ScriptedModel(holder["turns"])
        app = create_forge_app(ForgeMiniSettings(forge_data_dir=tmp_path / "data", openai_api_key=None, **settings),
                               db_settings=_db_settings(), model_factory=factory)
        return TestClient(app)

    def make_run(client, test_command):
        config = {} if test_command is None else {
            "source_path": str(project), "test_command": test_command, "image": default_image()}
        ws = client.post("/workspaces", json={"name": "e2e", **config}).json()
        created["workspaces"].append(ws["id"])
        task = client.post("/tasks", json={"workspace_id": ws["id"], "goal": "fix the failing tests",
                                           "acceptance_criteria": ["the test command passes"]}).json()
        run = client.post(f"/tasks/{task['id']}/runs").json()
        return task, run

    yield build, make_run, project

    async def cleanup():
        engine = create_engine(_db_settings())
        factory = create_session_factory(engine)
        async with factory.begin() as s:
            from sqlalchemy import select
            ids = [uuid_ for uuid_ in created["workspaces"]]
            task_ids = (await s.scalars(select(TaskModel.id).where(TaskModel.workspace_id.in_(ids)))).all()
            run_ids = (await s.scalars(select(AgentRunModel.id).where(AgentRunModel.task_id.in_(task_ids)))).all()
            await s.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id.in_(run_ids)))
            await s.execute(delete(AgentRunModel).where(AgentRunModel.id.in_(run_ids)))
            await s.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))
            await s.execute(delete(WorkspaceModel).where(WorkspaceModel.id.in_(ids)))
        await engine.dispose()
    asyncio.run(cleanup())


def wait_terminal(client, run_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/agent-runs/{run_id}").json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"run did not finish; last status {status}")



def _with_cache_split(turns, cached):
    """Same scripted turns, but the provider also says how many input tokens came from its cache."""
    return [t.model_copy(update={"usage_metadata": {**t.usage_metadata,
                                                     "input_token_details": {"cache_read": cached}}})
            for t in turns]


@pytest.mark.parametrize("turns,cost,status", [
    # 4 calls x (80 fresh input x $2 + 20 cached x $0.5 + 10 output x $8) per 1M tokens = $0.001
    (_with_cache_split(FIX_AVERAGE, 20), Decimal("0.001"), "estimated"),
    # cache split NOT reported and cached price differs from input price: Forge refuses to guess any call's cost
    (FIX_AVERAGE, None, "incomplete"),
], ids=["cache-split-known", "cache-split-unknown"])
def test_run_cost_is_estimated_from_the_owner_tariff(api, turns, cost, status):
    build, make_run, _ = api
    tariff = {"forge_model_name": "synthetic-model", "forge_price_input_usd_per_million": "2",
              "forge_price_output_usd_per_million": "8", "forge_price_cached_input_usd_per_million": "0.5"}
    with build(turns, **tariff) as client:
        _, run = make_run(client, AVG)
        assert client.post(f"/agent-runs/{run['id']}/start").status_code == 202
        assert wait_terminal(client, run["id"]) == "succeeded"
        usage = client.get(f"/agent-runs/{run['id']}/report").json()["usage"]
        assert usage["cost_status"] == status
        assert (None if usage["known_cost_usd"] is None else Decimal(usage["known_cost_usd"])) == cost
        result = client.get(f"/agent-runs/{run['id']}/result").json()["usage"]
        assert result["cost_complete"] is (status == "estimated") and result["known_total_tokens"] == 440

def test_full_happy_path_over_http(api):
    build, make_run, _ = api
    with build(FIX_AVERAGE) as client:
        task, run = make_run(client, AVG)
        assert client.post(f"/agent-runs/{run['id']}/start").status_code == 202
        assert wait_terminal(client, run["id"]) == "succeeded"

        report = client.get(f"/agent-runs/{run['id']}/report").json()
        assert report["run_status"] == "succeeded" and report["task_status"] == "succeeded"
        v = report["verification"]
        assert v["verdict"] == "pass" and v["reason"] == "tests_fail_before_and_pass_after"
        assert v["baseline"]["exit_code"] != 0 and v["patched"]["exit_code"] == 0
        assert v["changed_paths"] == ["calc/ops.py"] and report["agent_summary"] == "average used floor division"

        assert report["has_patch"] is True
        # 4 scripted model turns, each reporting 100 input / 10 output tokens, 4 tool calls (incl. finish)
        # no tariff configured: tokens are known, money is explicitly "not priced" (never shown as $0)
        assert report["usage"] == {"steps": 4, "tool_calls": 4, "input_tokens": 400, "output_tokens": 40,
                                   "known_cost_usd": None, "cost_status": "not_priced"}
        patch = client.get(f"/agent-runs/{run['id']}/patch")
        assert patch.status_code == 200 and "+    return sum(numbers) / len(numbers)" in patch.text

        # the OLD strict read API must understand the new events and results too
        result = client.get(f"/agent-runs/{run['id']}/result")
        assert result.status_code == 200
        assert result.json()["run_status"] == "succeeded" and result.json()["verification_verdict"] == "pass"
        # every sandbox model call reached the kernel UsageCollector (before C4-finding: usage was null here)
        assert result.json()["usage"] == {"known_total_tokens": 440, "usage_complete": True,
                                          "known_cost_usd": None, "cost_complete": False}
        events = client.get(f"/agent-runs/{run['id']}/events?limit=100").json()["items"]
        kinds = [e["kind"] for e in events]
        assert kinds.count("agent_step") >= 8 and "verification_recorded" in kinds
        step_details = [e["details"] for e in events if e["kind"] == "agent_step"]
        bash_runs = [d for d in step_details if d.get("tool") == "bash"]
        assert bash_runs[0]["exit_code"] != 0 and bash_runs[-1]["exit_code"] == 0      # red first, green at the end
        assert any(d.get("tool") == "edit_file" and d["target"] == "calc/ops.py" and d["ok"] for d in step_details)
        assert [d["outcome"] for d in step_details if d["step"] == "finish"] == ["claimed_done"]
        steps = [e["details"]["summary"] for e in events if e["kind"] == "agent_step"]
        assert any("FAILED" in s for s in steps), "the live log must show the agent seeing the red test"
        assert [e["details"]["to"] for e in events if e["kind"] == "agent_run_status_changed"] == \
            ["running", "verifying", "succeeded"]


def test_honest_failure_explains_why_and_task_is_failed(api):
    build, make_run, _ = api
    turns = [tool_turn("bash", {"command": DIV}),
             tool_turn("finish", {"status": "cannot_fix",
                                  "summary": "Requirement contradicts test_division_by_zero_raises; "
                                             "a human must decide which one is right."})]
    with build(turns) as client:
        task, run = make_run(client, DIV)
        client.post(f"/agent-runs/{run['id']}/start")
        assert wait_terminal(client, run["id"]) == "failed"
        report = client.get(f"/agent-runs/{run['id']}/report").json()
        assert report["task_status"] == "failed" and report["verification"] is None
        assert "contradicts test_division_by_zero_raises" in report["blocked_reason"]
        # live-run finding: the agent changed nothing, so there is no patch to offer (was: has_patch=true)
        assert report["has_patch"] is False
        assert report["usage"]["steps"] == 2
        assert client.get(f"/agent-runs/{run['id']}/patch").status_code == 404
        assert client.get(f"/agent-runs/{run['id']}/result").status_code == 200


def test_missing_model_configuration_is_explained_not_a_bare_503(api):
    build, make_run, _ = api
    with build([], key_present=False) as client:
        task, run = make_run(client, AVG)
        r = client.post(f"/agent-runs/{run['id']}/start")
        assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["error"]["message"]


def test_unconfigured_workspace_fails_with_a_clear_code(api):
    build, make_run, _ = api
    with build(FIX_AVERAGE) as client:
        _, run = make_run(client, None)                  # legacy workspace: no source_path / test_command
        client.post(f"/agent-runs/{run['id']}/start")
        assert wait_terminal(client, run["id"]) == "failed"
        report = client.get(f"/agent-runs/{run['id']}/report").json()
        assert report["failure_code"] == "workspace_not_configured" and report["task_status"] == "failed"


def test_two_workspaces_can_run_in_one_app_sequentially(api):
    build, make_run, _ = api
    with build(FIX_AVERAGE) as client:
        _, run1 = make_run(client, AVG)
        client.post(f"/agent-runs/{run1['id']}/start")
        assert wait_terminal(client, run1["id"]) == "succeeded"
        _, run2 = make_run(client, DIV)      # a different workspace id: M1's binding would have rejected this
        r = client.post(f"/agent-runs/{run2['id']}/start")
        assert r.status_code == 202
        wait_terminal(client, run2["id"])
