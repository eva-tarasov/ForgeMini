import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.tool import tool_call
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from model_test_support import offline_model_tests

from app.agent_runtime import repository_graph
from app.agent_runtime.agent_graph import MAX_MODEL_CALLS, AgentGraphError, run_agent_graph
from app.agent_runtime.repository_graph import create_repository_graph
from app.agent_runtime.repository_tool_schemas import create_repository_tool_model
from app.agent_runtime.structured_output import TaskAnalysis
from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.services import repository_agent_runner
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.repository_agent_runner import RepositoryAgentRunner, RepositoryFixtureBinding
from app.services.tool_calls import ToolCallService
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor
from app.tools.repository.filesystem import SafeRepositoryReader


CONTENT = "synthetic Ω fixture content\n"
GOAL = "Read disposable fixture evidence"
CRITERIA = ("Leave independent verification pending",)
READ_GRANT = frozenset({Capability.REPOSITORY_READ})


def _request(name="read_file", *, call_id="read-1", args=None):
    return tool_call(name=name, args={"path": "allowed.txt"} if args is None else args, id=call_id)


def _tool_reply(*requests, message_id="ai-1"):
    return AIMessage(content="", id=message_id, tool_calls=list(requests))


def _model(*replies):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = list(replies)
    return model


def _finalizer():
    return _model({
        "raw": AIMessage(content=""),
        "parsed": TaskAnalysis(summary="Preliminary fixture analysis", relevant_components=[],
                               assumptions=["Independent verification pending"], blocked_reason=None),
        "parsing_error": None,
    })


async def _run(graph):
    return await run_agent_graph(graph, goal=GOAL, acceptance_criteria=CRITERIA)


@pytest.fixture
def fixture_tools(tmp_path):
    (tmp_path / "allowed.txt").write_text(CONTENT)
    (tmp_path / "denied.txt").write_text("synthetic out-of-scope content")
    baseline = FixtureBaseline("fixture-v1", {"allowed.txt": "prior fixture text\n"})
    context = PermissionContext(uuid4(), uuid4(), READ_GRANT, read_scopes=(PathScope("allowed.txt"),))
    executor = create_fixture_executor(root=tmp_path, context=context, baseline=baseline)
    ledger = create_autospec(ToolCallService, instance=True)
    ledger.propose.return_value = object()
    return SimpleNamespace(root=tmp_path, context=context, baseline=baseline, executor=executor, ledger=ledger)


def _graph(case, model, finalizer=None):
    return create_repository_graph(
        model, finalizer if finalizer is not None else _finalizer(),
        AuditedToolExecutor(case.executor, case.ledger), case.context,
    )


def _chat_model():
    return ChatOpenAI(model="gpt-4o-mini", api_key="synthetic-key", max_retries=0, use_responses_api=False)


def test_repository_model_schemas_come_from_registered_tool_specs(fixture_tools):
    bound = create_repository_tool_model(_chat_model(), fixture_tools.executor)
    assert bound.kwargs["parallel_tool_calls"] is False
    functions = [tool["function"] for tool in bound.kwargs["tools"]]
    assert [function["name"] for function in functions] == ["list_files", "read_file", "search_files", "git_diff"]
    for function in functions:
        spec = fixture_tools.executor.get_tool_spec(function["name"])
        assert function["description"] == spec.description
        assert function["parameters"] == spec.input_schema.model_json_schema()
        assert function["parameters"]["additionalProperties"] is False
    assert set(functions[1]["parameters"]["properties"]) == {"path"}
    assert functions[2]["parameters"]["required"] == ["query"]
    assert functions[0]["parameters"]["properties"]["directory"]["default"] == "."


def test_repository_graph_passes_audited_tool_output_with_exact_call_id(fixture_tools, monkeypatch):
    execute = AsyncMock(wraps=AuditedToolExecutor(fixture_tools.executor, fixture_tools.ledger).execute)
    monkeypatch.setattr(AuditedToolExecutor, "execute", execute)
    model = _model(_tool_reply(_request()), AIMessage(content="Fixture research", id="ai-2"))
    finalizer = _finalizer()
    result = asyncio.run(_run(_graph(fixture_tools, model, finalizer)))

    execute.assert_awaited_once_with("read_file", {"path": "allowed.txt"}, context=fixture_tools.context)
    fixture_tools.ledger.start.assert_awaited_once()
    fixture_tools.ledger.finish.assert_awaited_once()
    history = model.ainvoke.await_args_list[1].args[0]
    message = history[-1]
    assert isinstance(message, ToolMessage)
    assert (message.tool_call_id, message.name, message.status) == ("read-1", "read_file", "success")
    expected = {"content": CONTENT, "path": "allowed.txt", "size_bytes": len(CONTENT.encode())}
    assert message.content == json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len({item.id for item in result["messages"]}) == len(result["messages"])
    assert (result["model_calls"], result["tool_calls"], result["finalization_calls"]) == (2, 1, 1)
    assert result["final_result"].observations == ()
    payload = json.loads(finalizer.ainvoke.await_args.args[0][-1].content)
    assert payload["analysis_notes"] == ["Fixture research"] and payload["observations"] == []
    assert CONTENT.strip() not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    ("requests", "reason"),
    [
        ([_request("unknown")], "unknown_tool"),
        ([_request("apply_patch", args={"patch": "synthetic patch"})], "unknown_tool"),
        ([_request("read_fixture")], "unknown_tool"),
        ([_request(call_id=None)], "invalid_tool_call_id"),
        ([_request(call_id="  ")], "invalid_tool_call_id"),
        ([_request(call_id="x" * 129)], "invalid_tool_call_id"),
        ([_request(), _request(call_id="read-2")], "invalid_tool_call_count"),
    ],
    ids=["unknown", "write", "synthetic", "missing-id", "blank-id", "long-id", "multiple"],
)
def test_repository_graph_rejects_requests_before_audit(fixture_tools, requests, reason):
    finalizer = _finalizer()
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(_graph(fixture_tools, _model(_tool_reply(*requests)), finalizer)))
    assert error.value.args == (reason,)
    fixture_tools.ledger.propose.assert_not_awaited()
    finalizer.ainvoke.assert_not_awaited()
    assert (fixture_tools.root / "allowed.txt").read_text() == CONTENT


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"path": 123}, "invalid_input"),
        ({"path": "allowed.txt", "root": "/synthetic", "read_scopes": ["."], "capabilities": ["repository.read"],
          "context": {}}, "invalid_input"),
        ({"path": "denied.txt"}, "permission_denied"),
        ({"path": "../allowed.txt"}, "permission_denied"),
        ({"path": ".env"}, "permission_denied"),
        ({"path": "/synthetic/allowed.txt"}, "permission_denied"),
    ],
    ids=["invalid-input", "injected-context", "denied", "traversal", "protected", "absolute"],
)
def test_repository_graph_fails_closed_through_executor(fixture_tools, monkeypatch, arguments, reason):
    def no_read(*args, **kwargs):
        pytest.fail("Rejected repository request reached a read handler")

    monkeypatch.setattr(SafeRepositoryReader, "read_text", no_read)
    finalizer = _finalizer()
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(_graph(fixture_tools, _model(_tool_reply(_request(args=arguments))), finalizer)))
    assert error.value.args == (reason,)
    fixture_tools.ledger.propose.assert_awaited_once()
    fixture_tools.ledger.start.assert_not_awaited()
    assert fixture_tools.ledger.finish.await_args.kwargs["error_code"].value == reason
    finalizer.ainvoke.assert_not_awaited()


def test_repository_graph_rejects_symlink_within_scope(tmp_path, monkeypatch):
    (tmp_path / "target.txt").write_text(CONTENT)
    (tmp_path / "link.txt").symlink_to("target.txt")
    context = PermissionContext(uuid4(), uuid4(), READ_GRANT, read_scopes=(PathScope("link.txt"),))
    executor = create_fixture_executor(root=tmp_path, context=context, baseline=FixtureBaseline("fixture", {}))
    case = SimpleNamespace(executor=executor, ledger=create_autospec(ToolCallService, instance=True), context=context)

    def no_read(*args, **kwargs):
        pytest.fail("Symlink target contents must never be read")

    monkeypatch.setattr(SafeRepositoryReader, "_read_bytes", no_read)
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(_graph(case, _model(_tool_reply(_request(args={"path": "link.txt"}))))))
    assert error.value.args == ("handler_failed",)
    case.ledger.start.assert_awaited_once()
    assert case.ledger.finish.await_args.kwargs["error_code"].value == "handler_failed"


@pytest.mark.parametrize(("same_message_id", "reason"), [
    (False, "tool_budget_exceeded"),
    (True, "duplicate_message_id"),
], ids=["tool-budget", "duplicate-message-id"])
def test_repository_graph_rejects_repeated_tool_request(fixture_tools, same_message_id, reason):
    model = _model(
        _tool_reply(_request()),
        _tool_reply(_request(call_id="read-2"), message_id="ai-1" if same_message_id else "ai-2"),
    )
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(_graph(fixture_tools, model)))
    assert error.value.args == (reason,)
    fixture_tools.ledger.start.assert_awaited_once()
    assert model.ainvoke.await_count == 2


def test_repository_tool_node_checks_model_budget_and_argument_type(fixture_tools):
    executor = AuditedToolExecutor(fixture_tools.executor, fixture_tools.ledger)
    reply = _tool_reply(_request())
    state = {"messages": [reply], "tool_calls": 0, "model_calls": MAX_MODEL_CALLS}
    with pytest.raises(AgentGraphError, match="^model_budget_exceeded$"):
        asyncio.run(repository_graph._call_repository_tool(state, executor, fixture_tools.context))
    state["model_calls"] = 1
    reply.tool_calls[0]["args"] = []
    with pytest.raises(AgentGraphError, match="^invalid_tool_arguments$"):
        asyncio.run(repository_graph._call_repository_tool(state, executor, fixture_tools.context))
    fixture_tools.ledger.propose.assert_not_awaited()


def test_repository_graph_rejects_oversize_tool_message(fixture_tools, monkeypatch):
    monkeypatch.setattr(repository_graph, "MAX_TOOL_MESSAGE_BYTES", 8)
    model = _model(_tool_reply(_request()))
    with pytest.raises(AgentGraphError) as error:
        asyncio.run(_run(_graph(fixture_tools, model)))
    assert error.value.args == ("tool_message_too_large",)
    model.ainvoke.assert_awaited_once()


def _session(request, workspace_id, *, run=None, task=True):
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.return_value = run
    session.get.return_value = (
        SimpleNamespace(id=request.task_id, workspace_id=workspace_id, goal=GOAL,
                        acceptance_criteria=list(CRITERIA), status="queued")
        if task else None
    )
    return session


@pytest.mark.parametrize("mismatch", ["missing-run", "wrong-task", "missing-task", "wrong-workspace", "non-running"])
def test_repository_runner_rejects_unbound_run_before_model_or_fixture_access(fixture_tools, monkeypatch, mismatch):
    binding = RepositoryFixtureBinding(
        fixture_tools.context.workspace_id, fixture_tools.root, fixture_tools.baseline,
        READ_GRANT, fixture_tools.context.read_scopes,
    )
    request = AgentRunRequest(uuid4(), uuid4(), GOAL, CRITERIA)
    run = SimpleNamespace(
        task_id=uuid4() if mismatch == "wrong-task" else request.task_id,
        status="cancelled" if mismatch == "non-running" else "running",
    )
    session = _session(
        request, uuid4() if mismatch == "wrong-workspace" else binding.workspace_id,
        run=None if mismatch == "missing-run" else run, task=mismatch != "missing-task",
    )
    bind = Mock()
    create_fixture = Mock()
    monkeypatch.setattr(repository_agent_runner, "create_repository_tool_model", bind)
    monkeypatch.setattr(repository_agent_runner, "create_fixture_executor", create_fixture)
    with pytest.raises(AgentRunnerError, match="^execution_failed$"):
        asyncio.run(RepositoryAgentRunner(Mock(return_value=session), _chat_model(), binding).run(request))
    session.__aexit__.assert_awaited_once()
    bind.assert_not_called()
    create_fixture.assert_not_called()


def test_repository_runner_isolates_concurrent_workspaces_and_contexts(tmp_path, monkeypatch):
    async def check():
        started = (asyncio.Event(), asyncio.Event())
        runners, requests, ledgers, contexts = [], [], [], []
        model_bindings = {}
        create_fixture = repository_agent_runner.create_fixture_executor

        def observe_binding(**kwargs):
            contexts.append(kwargs["context"])
            return create_fixture(**kwargs)

        monkeypatch.setattr(repository_agent_runner, "create_fixture_executor", observe_binding)
        monkeypatch.setattr(repository_agent_runner, "create_repository_tool_model", lambda model, _: model_bindings[id(model)])
        monkeypatch.setattr(repository_agent_runner, "create_structured_task_model", lambda _: _finalizer())
        monkeypatch.setattr(repository_agent_runner, "ToolCallService", lambda factory: factory.ledger)

        def compose(index):
            root = tmp_path / str(index)
            root.mkdir()
            own_path, other_path = f"{index}.txt", f"{1 - index}.txt"
            (root / own_path).write_text(f"Workspace {index}")
            (root / other_path).write_text("Outside this read scope")
            workspace_id = uuid4()
            request = AgentRunRequest(uuid4(), uuid4(), GOAL, CRITERIA)
            binding = RepositoryFixtureBinding(
                workspace_id, root, FixtureBaseline("fixture", {}), READ_GRANT, (PathScope(own_path),),
            )
            session = _session(request, workspace_id, run=SimpleNamespace(task_id=request.task_id, status="running"))
            ledger = create_autospec(ToolCallService, instance=True)
            factory = Mock(return_value=session, ledger=ledger)
            chat_model = _chat_model()
            tool_model = _model()

            async def respond(messages):
                session.__aexit__.assert_awaited_once()
                if not isinstance(messages[-1], ToolMessage):
                    started[index].set()
                    await started[1 - index].wait()
                    return _tool_reply(_request(args={"path": own_path}))
                assert json.loads(messages[-1].content)["content"] == f"Workspace {index}"
                return AIMessage(content="Workspace-local fixture research")

            tool_model.ainvoke.side_effect = respond
            model_bindings[id(chat_model)] = tool_model
            return RepositoryAgentRunner(factory, chat_model, binding), request, ledger, binding

        for index in range(2):
            runner, request, ledger, binding = compose(index)
            runners.append(runner)
            requests.append(request)
            ledgers.append((ledger, binding))
        async with asyncio.timeout(3):
            results = await asyncio.gather(*(runner.run(request) for runner, request in zip(runners, requests)))
        assert all(result.status == "ready_for_verification" for result in results)
        assert len(contexts) == 2 and contexts[0] is not contexts[1]
        for request, (ledger, binding) in zip(requests, ledgers):
            ledger.propose.assert_awaited_once()
            context = ledger.propose.await_args.args[0]
            assert context is next(item for item in contexts if item.run_id == request.run_id)
            assert context == PermissionContext(request.run_id, binding.workspace_id, READ_GRANT,
                                                read_scopes=binding.read_scopes)

    asyncio.run(check())
