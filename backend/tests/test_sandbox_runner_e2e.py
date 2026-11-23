"""End-to-end: SandboxRunner + real Docker + examples/demo-calc + scripted model (no LLM, no network)."""
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.sandbox.agent_loop import LoopBudget
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.changeset import Snapshot
from app.sandbox.docker_sandbox import DockerSandbox, default_image, docker_available
from app.sandbox.runner import SandboxJob, SandboxRunner
from app.services.agent_runner import AgentRunRequest
from sandbox_support import ScriptedModel, tool_turn

pytestmark = pytest.mark.skipif(not asyncio.run(docker_available()), reason="no Docker daemon available")

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-calc"
AVG_TEST = "python -m unittest tests.test_average"
DIV_TEST = "python -m unittest tests.test_divide"


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return h.hexdigest()


def make_runner(tmp_path, turns, test_command, budget=LoopBudget()):
    async def resolve(_req):
        return SandboxJob(DEMO, default_image(), test_command)
    store = FileArtifactStore(tmp_path / "artifacts")
    return SandboxRunner(model=ScriptedModel(turns), resolve_job=resolve, artifacts=store, budget=budget), store


def request(goal="fix"):
    return AgentRunRequest(task_id=uuid4(), run_id=uuid4(), goal=goal, acceptance_criteria=())


def run_it(runner, req):
    return asyncio.run(runner.run(req))


FIX_AVERAGE = [
    tool_turn("bash", {"command": AVG_TEST}),
    tool_turn("read_file", {"path": "calc/ops.py"}),
    tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "sum(numbers) // len(numbers)",
                            "new_str": "sum(numbers) / len(numbers)"}),
    tool_turn("bash", {"command": AVG_TEST}),
    tool_turn("finish", {"status": "done", "summary": "average used floor division"}),
]


def test_good_agent_fix_is_exported_and_passes_in_a_fresh_container(tmp_path):
    before = tree_hash(DEMO)
    runner, store = make_runner(tmp_path, FIX_AVERAGE, AVG_TEST)
    req = request()
    res = run_it(runner, req)

    assert res.status == "ready_for_verification" and res.relevant_components == ("calc/ops.py",)
    assert "NOT verified" in res.assumptions[0]
    assert tree_hash(DEMO) == before                         # host checkout untouched

    diff = store.patch_text(req.run_id)
    assert "-    return sum(numbers) // len(numbers)" in diff and "+    return sum(numbers) / len(numbers)" in diff
    transcript = json.loads((tmp_path / "artifacts" / str(req.run_id) / "transcript.json").read_text())
    first_test_run = next(e for e in transcript if e["kind"] == "tool_result")
    assert "FAILED" in first_test_run["result"]              # the agent really saw the red test first

    # This is what the Verifier will do on day 2: pristine source + saved changeset, FRESH container.
    async def verify():
        patched = store.load_changeset(req.run_id).apply_to(Snapshot.from_directory(DEMO))
        async with await DockerSandbox.start(image=default_image(), source=patched) as sb:
            return await sb.exec(AVG_TEST)
    r = asyncio.run(verify())
    assert r.exit_code == 0 and "OK" in r.output


def test_agent_that_claims_success_without_changing_anything_is_blocked(tmp_path):
    runner, _ = make_runner(tmp_path, [tool_turn("finish", {"status": "done", "summary": "all good"})], AVG_TEST)
    res = run_it(runner, request())
    assert res.status == "blocked" and res.blocked_reason == "agent_reported_done_but_changed_nothing"


def test_honest_cannot_fix_is_preserved_with_its_reason(tmp_path):
    turns = [tool_turn("bash", {"command": DIV_TEST}),
             tool_turn("finish", {"status": "cannot_fix",
                                  "summary": "Requirement contradicts test_division_by_zero_raises"})]
    res = run_it(make_runner(tmp_path, turns, DIV_TEST)[0], request())
    assert res.status == "blocked" and "contradicts test_division_by_zero_raises" in res.blocked_reason


def test_cheating_by_editing_the_test_is_visible_in_the_changeset(tmp_path):
    turns = [tool_turn("edit_file", {"path": "calc/ops.py", "old_str": "return a / b",
                                     "new_str": "return a / b if b else 0.0"}),
             tool_turn("edit_file", {"path": "tests/test_divide.py", "old_str": "assertRaises(ZeroDivisionError)",
                                     "new_str": "assertRaises(BaseException)"}),
             tool_turn("finish", {"status": "done", "summary": "made divide safe"})]
    runner, store = make_runner(tmp_path, turns, DIV_TEST)
    req = request()
    res = run_it(runner, req)
    assert res.status == "ready_for_verification"           # the runner cannot judge...
    assert "tests/test_divide.py" in res.relevant_components  # ...but the evidence is there for the Verifier


def test_budget_exhaustion_still_saves_artifacts_and_blocks(tmp_path):
    turns = [tool_turn("bash", {"command": "ls"}) for _ in range(5)]
    runner, store = make_runner(tmp_path, turns, AVG_TEST, budget=LoopBudget(max_steps=2))
    req = request()
    res = run_it(runner, req)
    assert res.status == "blocked" and "step_budget_exhausted" in res.blocked_reason
    assert store.load_changeset(req.run_id).is_empty


def test_no_container_is_left_behind(tmp_path):
    import subprocess
    run_it(make_runner(tmp_path, FIX_AVERAGE, AVG_TEST)[0], request())
    left = subprocess.run(["docker", "ps", "-aq", "--filter", "label=forge.sandbox=1"],
                          capture_output=True, text=True).stdout.strip()
    assert left == ""


# --- setup phase (ADR-004) ----------------------------------------------------------------------------------------
def _runner_with_setup(tmp_path, turns, setup, events):
    async def resolve(_req):
        return SandboxJob(DEMO, default_image(), AVG_TEST, setup)

    def emit_factory(_req):
        async def emit(kind, data):
            events.append((kind, data))
        return emit
    store = FileArtifactStore(tmp_path / "artifacts")
    return SandboxRunner(model=ScriptedModel(turns), resolve_job=resolve, artifacts=store,
                         emit_factory=emit_factory), store


def test_setup_phase_runs_before_the_agent_and_is_reported(tmp_path):
    import subprocess
    events: list = []
    setup = f"echo deps-installed  # {uuid4().hex}"
    runner, _ = _runner_with_setup(tmp_path, FIX_AVERAGE, setup, events)
    res = run_it(runner, request())
    assert res.status == "ready_for_verification"
    setup_events = [d for k, d in events if k == "setup"]
    assert [d["state"] for d in setup_events] == ["started", "built"]
    assert events[0][0] == "setup" and events[1][0] == "setup"                 # before the first model turn
    subprocess.run(["docker", "rmi", "-f", setup_events[1]["image"]], capture_output=True)


def test_failed_setup_stops_the_run_with_setup_failed_and_the_error_tail(tmp_path):
    from app.services.agent_runner import AgentRunnerError
    events: list = []
    setup = f"echo 'ERROR: Could not find a version that satisfies nope==9' >&2; exit 1  # {uuid4().hex}"
    runner, store = _runner_with_setup(tmp_path, FIX_AVERAGE, setup, events)
    req = request()
    with pytest.raises(AgentRunnerError) as info:
        run_it(runner, req)
    assert info.value.reason == "setup_failed"
    errors = [d["detail"] for k, d in events if k == "error"]
    assert len(errors) == 1 and "exit code 1" in errors[0] and "nope==9" in errors[0]
    assert not any(k == "model_response" for k, _ in events)                   # the agent never started
    assert not store.has_run(req.run_id)
