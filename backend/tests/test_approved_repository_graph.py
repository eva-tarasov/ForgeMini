import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from model_test_support import offline_model_tests
from test_approval_contracts import patch_for
from test_repository_agent_graph import CRITERIA, GOAL, _chat_model, _finalizer, _model, _request, _run, _session, _tool_reply

from app.agent_runtime.agent_graph import AgentGraphError
from app.agent_runtime.approved_repository_graph import create_approved_repository_graph
from app.agent_runtime.repository_tool_schemas import create_approved_repository_tool_model
from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.services import approved_repository_agent_runner as runner_module
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunRequest, AgentRunnerError
from app.services.approval_contracts import ApprovalDecision, PendingOperation, validate_patch_proposal
from app.services.approved_repository_agent_runner import ApprovedRepositoryAgentRunner, ApprovedRepositoryFixtureBinding
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_calls import ToolCallService
from app.tools.contracts import ToolRiskLevel
from app.tools.errors import ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor, create_fixture_patch_tool
from app.tools.repository.filesystem import SafeRepositoryReader
from app.tools.repository.schemas import ApplyPatchOutput, ReadFileOutput


GRANTS = frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE})
ALL_PATHS = (PathScope(".", recursive=True),)
FILE = "allowed.txt"
PATCH = patch_for(FILE)
ORIGINAL = "before\n"


@pytest.fixture
def fixture_executors(tmp_path):
    (tmp_path / FILE).write_text(ORIGINAL)
    (tmp_path / "denied.txt").write_text(ORIGINAL)
    (tmp_path / "link.txt").symlink_to(FILE)
    context = PermissionContext(uuid4(), uuid4(), GRANTS, ALL_PATHS, (PathScope(FILE), PathScope("link.txt")))
    reader = create_fixture_executor(root=tmp_path, context=context, baseline=FixtureBaseline("fixture", {}))
    writer = ToolExecutor(ToolRegistry([create_fixture_patch_tool(root=tmp_path, context=context)]))
    return SimpleNamespace(root=tmp_path, context=context, reader=reader, writer=writer)


def _write_request(*, call_id="write-1", **arguments):
    return _request("apply_patch", call_id=call_id, args=arguments or {"patch": PATCH})


def test_approved_repository_schema_comes_from_five_registered_specs(fixture_executors):
    case = fixture_executors
    bound = create_approved_repository_tool_model(_chat_model(), case.reader, case.writer)
    assert bound.kwargs["parallel_tool_calls"] is False
    functions = [tool["function"] for tool in bound.kwargs["tools"]]
    assert [function["name"] for function in functions] == [
        "list_files", "read_file", "search_files", "git_diff", "apply_patch",
    ]
    for function in functions:
        executor = case.writer if function["name"] == "apply_patch" else case.reader
        spec = executor.get_tool_spec(function["name"])
        assert function["description"] == spec.description
        assert function["parameters"] == spec.input_schema.model_json_schema()
    assert set(functions[-1]["parameters"]["properties"]) == {"patch"}
    assert functions[-1]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("tampering", [
    {"version": 2}, {"capability": Capability.REPOSITORY_READ}, {"risk_level": ToolRiskLevel.LOW},
    {"side_effect": False}, {"requires_approval": False},
])
def test_approved_repository_schema_rejects_mismatched_write_metadata(fixture_executors, tampering):
    case = fixture_executors
    forged = case.writer.get_tool_spec("apply_patch").model_copy(update=tampering)
    with pytest.raises(ValueError, match="^repository_model_requires_approved_patch_tool$"):
        create_approved_repository_tool_model(_chat_model(), case.reader, Mock(get_tool_spec=lambda _: forged))


def _routes():
    return (
        SimpleNamespace(execute=AsyncMock(return_value=ReadFileOutput(path=FILE, content=ORIGINAL, size_bytes=7))),
        SimpleNamespace(execute=AsyncMock(return_value=ApplyPatchOutput(changed_paths=(FILE,), changed_files=1))),
    )


@pytest.mark.parametrize("with_read", [True, False], ids=["read-then-write", "write-only"])
def test_approved_graph_routes_sequential_tools_and_preserves_message_ids(fixture_executors, with_read):
    context = fixture_executors.context
    reader, writer = _routes()
    replies = [_tool_reply(_request(), message_id="read-ai")] if with_read else []
    replies += [_tool_reply(_write_request(), message_id="write-ai"), AIMessage(content="Fixture results")]
    model = _model(*replies)
    finalizer = _finalizer()
    result = asyncio.run(_run(create_approved_repository_graph(model, finalizer, reader, writer, context)))
    if with_read:
        reader.execute.assert_awaited_once_with("read_file", {"path": FILE}, context=context)
    else:
        reader.execute.assert_not_awaited()
    writer.execute.assert_awaited_once_with("apply_patch", {"patch": PATCH}, context=context)
    messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    expected = [("read_file", "read-1", "success")] if with_read else []
    assert [(item.name, item.tool_call_id, item.status) for item in messages] == [*expected, ("apply_patch", "write-1", "success")]
    assert json.loads(messages[-1].content) == {"changed_paths": [FILE], "changed_files": 1}
    assert len({message.id for message in result["messages"]}) == len(result["messages"])
    assert result["final_result"].observations == () and result["final_result"].status == "ready_for_verification"
    assert result["model_calls"] == (3 if with_read else 2)
    assert result["tool_calls"] == (2 if with_read else 1)
    finalizer.ainvoke.assert_awaited_once()


@pytest.mark.parametrize(("turns", "reason", "reads", "writes"), [
    ([[_write_request()], [_write_request(call_id="again")]], "tool_after_write", 0, 1),
    ([[_write_request()], [_request()]], "tool_after_write", 0, 1),
    ([[_request()], [_write_request(call_id="read-1")]], "duplicate_tool_call_id", 1, 0),
    ([[_request(), _write_request()]], "invalid_tool_call_count", 0, 0),
    ([[_write_request(call_id=None)]], "invalid_tool_call_id", 0, 0),
    ([[_write_request(call_id="x" * 129)]], "invalid_tool_call_id", 0, 0),
    ([[_request("shell")]], "unknown_tool", 0, 0),
    ([[_request()], [_request(call_id="second")], [_write_request()]], "tool_budget_exceeded", 2, 0),
], ids=["repeat-write", "read-after-write", "duplicate-call-id", "parallel", "missing-id", "oversized-id",
        "unknown", "budget"])
def test_approved_graph_rejects_unadmitted_tool_requests(fixture_executors, turns, reason, reads, writes):
    reader, writer = _routes()
    model = _model(*[_tool_reply(*calls, message_id=f"ai-{index}") for index, calls in enumerate(turns)])
    finalizer = _finalizer()
    graph = create_approved_repository_graph(model, finalizer, reader, writer, fixture_executors.context)
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(graph))
    assert error.value.args == (reason,)
    assert reader.execute.await_count == reads and writer.execute.await_count == writes
    assert model.ainvoke.await_count <= 3
    finalizer.ainvoke.assert_not_awaited()


@pytest.mark.parametrize(("proposal", "reason"), [
    (_write_request(patch="invalid unified patch"), "invalid_input"),
    (_write_request(patch=PATCH, approved=True, root="/synthetic", scopes=["."], capabilities=["repository.write"]),
     "invalid_input"),
    (_write_request(patch=patch_for("denied.txt")), "permission_denied"),
    (_write_request(patch=patch_for("../allowed.txt")), "invalid_input"),
    (_write_request(patch=patch_for(".env")), "invalid_input"),
    (_write_request(patch=patch_for("link.txt")), "approval_source_changed"),
], ids=["malformed", "forged-context", "scope", "traversal", "protected", "symlink"])
def test_approved_graph_rejects_unsafe_patches_before_approval(fixture_executors, monkeypatch, proposal, reason):
    case = fixture_executors
    sessions = Mock()
    writer = ApprovedToolExecutor(case.writer, sessions)
    reader, _ = _routes()
    finalizer = _finalizer()

    def forbidden_read(*args, **kwargs):
        pytest.fail("Patch read attempted through an unapproved path")

    monkeypatch.setattr(SafeRepositoryReader, "_read_bytes", forbidden_read)
    graph = create_approved_repository_graph(_model(_tool_reply(proposal)), finalizer, reader, writer, case.context)
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(graph))
    assert error.value.args == (reason,)
    sessions.assert_not_called()
    finalizer.ainvoke.assert_not_awaited()
    assert (case.root / FILE).read_text() == ORIGINAL


def test_approved_graph_read_denial_stays_in_audited_read_route(fixture_executors):
    case = fixture_executors
    context = replace(case.context, read_scopes=(PathScope(FILE),))
    executor = create_fixture_executor(root=case.root, context=context, baseline=FixtureBaseline("fixture", {}))
    audit = Mock(propose=AsyncMock(), start=AsyncMock(), finish=AsyncMock(), spec=ToolCallService)
    audited = AuditedToolExecutor(executor, audit)
    _, writer = _routes()
    model = _model(_tool_reply(_request(args={"path": "denied.txt"})))
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(create_approved_repository_graph(model, _finalizer(), audited, writer, context)))
    assert error.value.args == ("permission_denied",)
    assert audit.finish.await_args.kwargs["error_code"].value == "permission_denied"
    audit.start.assert_not_called()
    writer.execute.assert_not_called()


@pytest.mark.parametrize("mismatch", ["run", "task", "workspace", "status"])
def test_approved_runner_checks_binding_before_requesting_model(fixture_executors, monkeypatch, mismatch):
    case = fixture_executors
    request = AgentRunRequest(uuid4(), uuid4(), GOAL, CRITERIA)
    binding = ApprovedRepositoryFixtureBinding(case.context.workspace_id, case.root, FixtureBaseline("fixture", {}),
                                               GRANTS, ALL_PATHS, ALL_PATHS)
    row = SimpleNamespace(task_id=uuid4() if mismatch == "task" else request.task_id,
                          status="cancelled" if mismatch == "status" else "running")
    session = _session(request, uuid4() if mismatch == "workspace" else binding.workspace_id, run=row)
    factory = Mock(return_value=session)
    make_graph = Mock()
    monkeypatch.setattr(runner_module, "create_approved_repository_graph", make_graph)
    runner = ApprovedRepositoryAgentRunner(factory, _chat_model(), binding,
                                          run_id=uuid4() if mismatch == "run" else request.run_id)
    with pytest.raises(AgentRunnerError, match="^execution_failed$"):
        asyncio.run(runner.run(request))
    make_graph.assert_not_called()


def test_approved_runner_isolates_concurrent_preview_handles_and_clears_them(tmp_path, monkeypatch):
    async def check():
        ready = [asyncio.Event(), asyncio.Event()]
        resume = asyncio.Event()

        def fake_graph(_model, _structured, reader, writer, context):
            return reader, writer, context

        async def wait_on_fake_graph(runner, request):
            reader, writer, context = runner._graph
            index = next(index for index, item in enumerate(requests) if item.run_id == request.run_id)
            sessions[index].__aexit__.assert_awaited_once()
            bound_read = reader._executor._registry.get("read_file")
            bound_write = writer._executor._registry.get("apply_patch")
            assert bound_read.bound_context is bound_write.bound_context is context
            assert context.workspace_id == runners[index]._binding.workspace_id and context.run_id == request.run_id
            assert context.capabilities == GRANTS and context.write_scopes == (PathScope(FILE),)
            patch = patch_for(FILE, after=f"fixture-{index}")
            tool, typed, safe_writer = validate_patch_proposal(
                writer._executor, "apply_patch", {"patch": patch}, version=1, context=context,
            )
            assert safe_writer._reader._root == runners[index]._binding.root.resolve()
            operation = PendingOperation.create(context, tool, safe_writer, typed, safe_writer.preview(patch),
                                                asyncio.get_running_loop().time() + 3)
            writer._gate.decide = decisions[index]
            writer._pending.reserve(operation)
            writer._pending.publish(operation)
            ready[index].set()
            try:
                await resume.wait()
                return AgentFinalResult(summary="Preliminary", status="ready_for_verification", relevant_components=(),
                                        assumptions=(), observations=(), blocked_reason=None)
            finally:
                writer._pending.discard(operation)

        monkeypatch.setattr(runner_module, "create_approved_repository_graph", fake_graph)
        monkeypatch.setattr(runner_module.LangGraphAgentRunner, "run", wait_on_fake_graph)
        runners, requests, sessions = [], [], []
        decisions = [AsyncMock(), AsyncMock()]
        for index in range(2):
            root = tmp_path / str(index)
            root.mkdir()
            (root / FILE).write_text(ORIGINAL)
            request = AgentRunRequest(uuid4(), uuid4(), GOAL, CRITERIA)
            workspace = uuid4()
            row = SimpleNamespace(task_id=request.task_id, status="running")
            session = _session(request, workspace, run=row)
            binding = ApprovedRepositoryFixtureBinding(workspace, root, FixtureBaseline("fixture", {}), GRANTS,
                                                       ALL_PATHS, (PathScope(FILE),))
            runners.append(ApprovedRepositoryAgentRunner(Mock(return_value=session), _chat_model(), binding,
                                                         run_id=request.run_id))
            requests.append(request)
            sessions.append(session)

        jobs = [asyncio.create_task(runner.run(request)) for runner, request in zip(runners, requests)]
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*(signal.wait() for signal in ready))
                first, second = (runner._approval_preview() for runner in runners)
                assert first.patch != second.patch and first.reference != second.reference
                for invalid in (second, replace(first, patch=second.patch)):
                    with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                        await runners[0]._decide(invalid, ApprovalDecision.APPROVE)
                decisions[0].assert_not_awaited()
                await runners[0]._decide(first, ApprovalDecision.REJECT)
                decisions[0].assert_awaited_once_with(first.reference, ApprovalDecision.REJECT,
                                                      context=runners[0]._active[0])
                jobs[1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await jobs[1]
                resume.set()
                await jobs[0]
                assert all(runner._approval_preview() is None for runner in runners)
                with pytest.raises(AgentRunnerError, match="^execution_failed$"):
                    await runners[0].run(requests[0])
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    asyncio.run(check())
