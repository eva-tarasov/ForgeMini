import asyncio
import json
from copy import deepcopy
from functools import wraps
from unittest.mock import AsyncMock, create_autospec, patch
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import tool_call
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from model_test_support import offline_model_tests

from app.agent_runtime.agent_graph import (
    GRAPH_RECURSION_LIMIT,
    MAX_MODEL_CALLS,
    AgentGraphError,
    call_model,
    call_tool,
    create_agent_graph,
    initialize,
    run_agent_graph,
)
from app.agent_runtime.fixture_tools import create_tool_calling_model, read_fixture
from app.agent_runtime.structured_output import TaskAnalysis


GOAL = "Проверить область действия ключа"
CRITERIA = ("Отделить fixture от предположений",)
FIXTURE_TEXT = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)
USAGE = {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}


@pytest.fixture
def fixture_execution(monkeypatch):
    original = read_fixture.coroutine
    execution = AsyncMock(wraps=original)

    @wraps(original)
    async def observed(**kwargs):
        return await execution(**kwargs)

    monkeypatch.setattr(read_fixture, "coroutine", observed)
    return execution


def _model(*replies):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = list(replies)
    return model


def _request(*, name="read_fixture", call_id="fixture-1", args=None):
    return tool_call(
        name=name,
        id=call_id,
        args={"component": "task_service"} if args is None else args,
    )


def _tool_reply(*calls, message_id=None):
    return AIMessage(content="", id=message_id, tool_calls=list(calls))


@pytest.fixture
def structured_model():
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = {
        "raw": AIMessage(content=""),
        "parsed": TaskAnalysis(
            summary="Synthetic analysis",
            relevant_components=[],
            assumptions=[],
            blocked_reason=None,
        ),
        "parsing_error": None,
    }
    return model


def _run(model, structured_model):
    graph = create_agent_graph(model, structured_model)
    return asyncio.run(run_agent_graph(graph, goal=GOAL, acceptance_criteria=CRITERIA))


def test_agent_graph_initialize_returns_only_new_messages_and_zero_deltas():
    inputs = {"goal": GOAL, "acceptance_criteria": list(CRITERIA)}
    before = deepcopy(inputs)

    update = initialize(inputs)

    assert inputs == before
    assert set(update) == {"messages", "model_calls", "tool_calls", "observations", "finalization_calls"}
    assert update["model_calls"] == update["tool_calls"] == update["finalization_calls"] == 0
    assert update["observations"] == []
    system, human = update["messages"]
    assert isinstance(system, SystemMessage)
    assert "synthetic training data" in system.content
    assert "remain unverified" in system.content
    assert isinstance(human, HumanMessage)
    assert json.loads(human.content) == before


def test_agent_graph_factory_does_not_call_model(structured_model):
    model = _model()

    create_agent_graph(model, structured_model)

    model.ainvoke.assert_not_awaited()
    model.invoke.assert_not_called()
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_without_tool_calls_preserves_provided_message(fixture_execution, structured_model):
    reply = AIMessage(content="Direct response", id="assistant-direct", usage_metadata=USAGE)
    model = _model(reply)

    result = _run(model, structured_model)

    assert (result["model_calls"], result["tool_calls"]) == (1, 0)
    assert result["finalization_calls"] == 1
    assert result["final_result"].observations == ()
    assert [type(message) for message in result["messages"]] == [SystemMessage, HumanMessage, AIMessage]
    assert result["messages"][-1] is reply
    assert result["messages"][-1].usage_metadata == USAGE
    assert (result["goal"], result["acceptance_criteria"]) == (GOAL, list(CRITERIA))
    model.ainvoke.assert_awaited_once()
    structured_model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()


def test_agent_graph_accepts_nonempty_text_block(fixture_execution, structured_model):
    content = [{"type": "text", "text": "Valid analysis"}]
    response_metadata = {"finish_reason": "stop"}
    reply = AIMessage(content=content, response_metadata=response_metadata, usage_metadata=USAGE)
    model = _model(reply)

    result = _run(model, structured_model)

    assert (result["model_calls"], result["tool_calls"]) == (1, 0)
    assert result["finalization_calls"] == 1
    assert len(result["messages"]) == 3
    final = result["messages"][-1]
    assert isinstance(final, AIMessage)
    assert final.content == content
    assert final.response_metadata == response_metadata
    assert final.usage_metadata == USAGE
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()


def test_agent_graph_model_node_copies_missing_id_and_returns_delta():
    reply = AIMessage(
        content="Final response",
        response_metadata={"finish_reason": "stop"},
        usage_metadata=USAGE,
    )
    model = _model(reply)
    state = {
        "goal": GOAL,
        "acceptance_criteria": list(CRITERIA),
        "messages": [HumanMessage(content=GOAL, id="user-message")],
        "model_calls": 1,
        "tool_calls": 0,
    }
    before = deepcopy(state)

    update = asyncio.run(call_model(state, model))

    assert set(update) == {"model_calls", "messages"}
    assert update["model_calls"] == 1
    (copied,) = update["messages"]
    assert UUID(copied.id).version == 4
    assert copied is not reply
    assert copied.model_dump(exclude={"id"}) == reply.model_dump(exclude={"id"})
    assert reply.id is None
    assert state == before
    model.ainvoke.assert_awaited_once_with(state["messages"])


@pytest.mark.parametrize(
    "reply,reason",
    [
        (HumanMessage(content="unexpected"), "invalid_model_message"),
        (
            AIMessage(content="", invalid_tool_calls=[{"name": "read_fixture", "args": "{"}]),
            "invalid_tool_call",
        ),
        (AIMessage(content="Partial text", additional_kwargs={"refusal": "Synthetic refusal"}), "model_refusal"),
        (AIMessage(content="Partial text", response_metadata={"finish_reason": "length"}), "incomplete_model_response"),
        (AIMessage(content="Filtered", response_metadata={"finish_reason": "content_filter"}), "incomplete_model_response"),
        (AIMessage(content=" \t"), "empty_model_response"),
        (AIMessage(content=[]), "empty_model_response"),
        (AIMessage(content=["   "]), "empty_model_response"),
        (AIMessage(content=[{"type": "text", "text": ""}]), "empty_model_response"),
        (
            AIMessage(content=[{"type": "reasoning", "reasoning": "Synthetic reasoning"}]),
            "empty_model_response",
        ),
        (AIMessage(content="Valid text", id=" "), "invalid_message_id"),
    ],
    ids=[
        "type", "malformed-call", "refusal", "length", "filter", "blank", "empty-list",
        "blank-list-text", "empty-text-block", "reasoning-only", "blank-id",
    ],
)
def test_agent_graph_rejects_invalid_model_reply(reply, reason, fixture_execution, structured_model):
    model = _model(reply)

    with pytest.raises(AgentGraphError) as exc:
        _run(model, structured_model)

    assert exc.value.args == (reason,)
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_propagates_provider_exception(fixture_execution, structured_model):
    timeout = TimeoutError("Synthetic model timeout")
    model = _model(timeout)

    with pytest.raises(TimeoutError) as exc:
        _run(model, structured_model)

    assert exc.value is timeout
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_checks_model_budget_before_invocation():
    model = _model()
    state = {
        "goal": GOAL,
        "acceptance_criteria": list(CRITERIA),
        "messages": [HumanMessage(content=GOAL, id="user-message")],
        "model_calls": MAX_MODEL_CALLS,
        "tool_calls": 0,
    }

    with pytest.raises(AgentGraphError, match="^model_budget_exceeded$"):
        asyncio.run(call_model(state, model))

    model.ainvoke.assert_not_awaited()


@pytest.mark.parametrize(
    "calls,reason",
    [
        ([_request(name="unknown")], "unknown_tool"),
        ([_request(call_id=None)], "invalid_tool_call_id"),
        ([_request(call_id=" \t")], "invalid_tool_call_id"),
        ([_request(args={"component": "synthetic-invalid-input"})], "invalid_tool_arguments"),
        ([_request(args={"component": "task_service", "extra": "synthetic-invalid-input"})], "invalid_tool_arguments"),
        ([_request(), _request(call_id="fixture-2")], "invalid_tool_call_count"),
    ],
    ids=["unknown-tool", "missing-id", "blank-id", "invalid-component", "extra-argument", "batch"],
)
def test_agent_graph_rejects_invalid_tool_request(calls, reason, fixture_execution, structured_model):
    model = _model(_tool_reply(*calls))

    with pytest.raises(AgentGraphError) as exc:
        _run(model, structured_model)

    assert exc.value.args == (reason,)
    assert exc.value.__cause__ is None
    assert "synthetic-invalid-input" not in str(exc.value)
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_rejects_tool_budget_exceeded(fixture_execution, structured_model):
    model = _model(_tool_reply(_request()), _tool_reply(_request(call_id="fixture-2")))

    with pytest.raises(AgentGraphError) as exc:
        _run(model, structured_model)

    assert exc.value.args == ("tool_budget_exceeded",)
    assert model.ainvoke.await_count == 2
    fixture_execution.assert_awaited_once_with(component="task_service")
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_rejects_tool_without_remaining_model_budget(fixture_execution):
    state = {
        "goal": GOAL,
        "acceptance_criteria": list(CRITERIA),
        "messages": [_tool_reply(_request())],
        "model_calls": MAX_MODEL_CALLS,
        "tool_calls": 0,
    }

    with pytest.raises(AgentGraphError, match="^model_budget_exceeded$"):
        asyncio.run(call_tool(state))

    fixture_execution.assert_not_awaited()


@pytest.mark.parametrize(
    "observation,reason",
    [
        ("not a ToolMessage", "invalid_tool_message"),
        (ToolMessage(content="Fixture", tool_call_id="wrong"), "tool_call_id_mismatch"),
        (ToolMessage(content="Error", tool_call_id="fixture-1", status="error"), "tool_execution_failed"),
        (ToolMessage(content="Fixture", tool_call_id="fixture-1", id="assistant-request"), "duplicate_message_id"),
    ],
    ids=["type", "correlation", "status", "duplicate-id"],
)
def test_agent_graph_rejects_invalid_tool_observation(observation, reason, structured_model):
    model = _model(_tool_reply(_request(), message_id="assistant-request"))

    with patch.object(type(read_fixture), "ainvoke", return_value=observation) as execute:
        with pytest.raises(AgentGraphError) as exc:
            _run(model, structured_model)

    assert exc.value.args == (reason,)
    execute.assert_awaited_once_with(_request())
    model.ainvoke.assert_awaited_once()
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_rejects_duplicate_message_id(fixture_execution, structured_model):
    first = _tool_reply(_request(), message_id="assistant-request")
    final = AIMessage(content="Would replace the tool request", id=first.id)
    model = create_autospec(Runnable, instance=True)
    snapshots = []

    async def reply(messages):
        snapshots.append(list(messages))
        return first if len(snapshots) == 1 else final

    model.ainvoke.side_effect = reply

    with pytest.raises(AgentGraphError) as exc:
        _run(model, structured_model)

    assert exc.value.args == ("duplicate_message_id",)
    assert [len(history) for history in snapshots] == [2, 4]
    assert snapshots[1][2] is first
    assert first.tool_calls == [_request()]
    assert snapshots[1][3].tool_call_id == "fixture-1"
    fixture_execution.assert_awaited_once_with(component="task_service")
    assert model.ainvoke.await_count == 2
    structured_model.ainvoke.assert_not_awaited()


def test_agent_graph_independent_invocations_on_same_compiled_graph(fixture_execution, structured_model):
    async def reply(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content=json.loads(messages[1].content)["goal"])
        return _tool_reply(_request())

    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = reply
    graph = create_agent_graph(model, structured_model)

    async def run_both():
        first = await run_agent_graph(graph, goal="Первая задача", acceptance_criteria=("Первый критерий",))
        before = deepcopy(first)
        second = await run_agent_graph(graph, goal="Вторая задача", acceptance_criteria=("Второй критерий",))
        return first, before, second

    first, before, second = asyncio.run(run_both())

    assert first == before
    assert model.ainvoke.await_count == 4
    assert fixture_execution.await_count == 2
    for result, goal, criterion in (
        (first, "Первая задача", "Первый критерий"),
        (second, "Вторая задача", "Второй критерий"),
    ):
        assert (result["model_calls"], result["tool_calls"]) == (2, 1)
        assert result["finalization_calls"] == 1
        assert len(result["messages"]) == 5
        assert json.loads(result["messages"][1].content) == {"goal": goal, "acceptance_criteria": [criterion]}
        assert result["messages"][-1].content == goal
        assert len({message.id for message in result["messages"]}) == 5


def test_agent_graph_ainvoke_applies_recursion_limit_once(monkeypatch, structured_model):
    graph = create_agent_graph(_model(AIMessage(content="Direct response")), structured_model)
    invoke = AsyncMock(wraps=graph.ainvoke)
    monkeypatch.setattr(graph, "ainvoke", invoke)
    criteria = [*CRITERIA]

    result = asyncio.run(run_agent_graph(graph, goal=GOAL, acceptance_criteria=criteria))

    invoke.assert_awaited_once_with(
        {"goal": GOAL, "acceptance_criteria": list(CRITERIA)},
        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
    )
    assert invoke.await_args.args[0]["acceptance_criteria"] is not criteria
    assert result["messages"][-1].content == "Direct response"


def test_agent_graph_openai_mock_transport(fixture_execution, structured_model):
    # Exercise the two research requests here. The full provider test covers finalization too.
    call_id = "fixture-http-1"
    arguments = {"component": "task_service"}
    provider_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_fixture", "arguments": json.dumps(arguments)},
    }
    final_text = "The training fixture scopes the key to a workspace."
    responses = iter([
        (
            "tool_calls",
            {"role": "assistant", "content": None, "tool_calls": [provider_call]},
            {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        ),
        (
            "stop",
            {"role": "assistant", "content": final_text},
            {"prompt_tokens": 24, "completion_tokens": 10, "total_tokens": 34},
        ),
    ])
    requests = []
    inputs = {"goal": GOAL, "acceptance_criteria": list(CRITERIA)}
    before = deepcopy(inputs)

    def respond(request):
        assert request.method == "POST"
        assert request.url == httpx.URL("https://forge-test.invalid/v1/chat/completions")
        requests.append(json.loads(request.content))
        finish_reason, message, usage = next(responses)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-graph-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
                "usage": usage,
            },
        )

    async def invoke():
        transport = httpx.MockTransport(respond)
        with httpx.Client(transport=transport, trust_env=False) as sync_client:
            async with httpx.AsyncClient(transport=transport, trust_env=False) as async_client:
                model = ChatOpenAI(
                    model="gpt-4o-mini",
                    api_key="synthetic-openai-key",
                    base_url="https://forge-test.invalid/v1",
                    openai_proxy="",
                    use_responses_api=False,
                    max_retries=0,
                    http_client=sync_client,
                    http_async_client=async_client,
                )
                graph = create_agent_graph(create_tool_calling_model(model), structured_model)
                return await run_agent_graph(graph, **inputs)

    result = asyncio.run(invoke())

    assert inputs == before
    assert {key: result[key] for key in inputs} == before
    assert (result["model_calls"], result["tool_calls"]) == (2, 1)
    assert result["finalization_calls"] == 1
    structured_model.ainvoke.assert_awaited_once()
    assert len(requests) == 2
    fixture_execution.assert_awaited_once_with(component="task_service")
    first, second = requests
    assert [message["role"] for message in first["messages"]] == ["system", "user"]
    assert "predefined synthetic training data" in first["messages"][0]["content"]
    assert json.loads(first["messages"][1]["content"]) == before
    assert first["parallel_tool_calls"] is False
    (definition,) = first["tools"]
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "read_fixture"
    schema = definition["function"]["parameters"]
    assert schema["required"] == ["component"]
    assert set(schema["properties"]) == {"component"}
    assert set(schema["properties"]["component"]["enum"]) == {"task_service", "agent_run_service"}

    assert [message["role"] for message in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][:2] == first["messages"]
    (assistant_call,) = second["messages"][2]["tool_calls"]
    assert assistant_call["id"] == provider_call["id"]
    assert assistant_call["type"] == provider_call["type"]
    assert assistant_call["function"]["name"] == provider_call["function"]["name"]
    assert json.loads(assistant_call["function"]["arguments"]) == arguments
    assert second["messages"][3] == {"role": "tool", "tool_call_id": call_id, "content": FIXTURE_TEXT}

    history = result["messages"]
    assert [type(message) for message in history] == [
        SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessage,
    ]
    assert len({message.id for message in history}) == 5
    assert isinstance(history[2], AIMessage)
    assert history[2].text == ""
    assert history[2].tool_calls == [_request(call_id=call_id, args=arguments)]
    assert history[3].content == FIXTURE_TEXT
    assert history[3].tool_call_id == call_id
    assert UUID(history[3].id).version == 4
    assert history[3].id != call_id
    assert history[4].content == final_text
    assert history[4].tool_calls == []
    for message, finish_reason, tokens in (
        (history[2], "tool_calls", (12, 8, 20)),
        (history[4], "stop", (24, 10, 34)),
    ):
        assert message.response_metadata["finish_reason"] == finish_reason
        usage = message.usage_metadata
        assert usage is not None
        assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == tokens
