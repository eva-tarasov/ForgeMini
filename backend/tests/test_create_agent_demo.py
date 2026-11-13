import asyncio
import json
from functools import wraps
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import OpenAITimeoutError
from model_test_support import offline_model_tests
from openai import APITimeoutError

from app.agent_runtime.create_agent_demo import (
    CreateAgentDemoError,
    create_fixture_agent,
    run_create_agent_demo,
)
from app.agent_runtime.model import build_task_messages
from app.agent_runtime.tool_calling_demo import read_fixture


GOAL = "Проверить область действия ключа"
CRITERIA = ("Отделить fixture от предположений",)
FIXTURE_TEXT = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)
FINAL_TEXT = "The training fixture scopes the key to a workspace."


def _tool_call(call_id="fixture-1", component="task_service"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_fixture", "arguments": json.dumps({"component": component})},
    }


def _tool_reply(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _final_reply():
    return {"role": "assistant", "content": FINAL_TEXT}


@pytest.fixture
def fixture_execution(monkeypatch):
    original = read_fixture.coroutine
    spy = AsyncMock(wraps=original)

    # ToolNode inspects the coroutine's annotations when it builds the graph.
    @wraps(original)
    async def observed(**kwargs):
        return await spy(**kwargs)

    monkeypatch.setattr(read_fixture, "coroutine", observed)
    return spy


@pytest.fixture
def mock_provider():
    requests = []

    def run(replies, *, factory=create_fixture_agent, runs=1):
        replies = iter(replies)

        def respond(request):
            assert request.method == "POST"
            assert request.url == httpx.URL("https://forge-test.invalid/v1/chat/completions")
            requests.append(json.loads(request.content))
            index = len(requests)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(
                200,
                json={
                    "id": f"chatcmpl-agent-{index}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls" if "tool_calls" in reply else "stop",
                            "message": reply,
                        },
                    ],
                    "usage": {
                        "prompt_tokens": 12 * index,
                        "completion_tokens": 8,
                        "total_tokens": 12 * index + 8,
                    },
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
                    agent = factory(model)
                    return [
                        await run_create_agent_demo(agent, goal=GOAL, acceptance_criteria=CRITERIA)
                        for _ in range(runs)
                    ]

        return asyncio.run(invoke())

    return run, requests


def test_create_fixture_agent_does_not_call_model_at_construction(mock_provider):
    run, requests = mock_provider

    run([], runs=0)

    assert requests == []


def test_create_agent_demo_without_tool_calls(mock_provider, fixture_execution):
    run, requests = mock_provider

    (history,) = run([_final_reply()])

    assert len(requests) == 1
    fixture_execution.assert_not_awaited()
    assert [type(message) for message in history] == [HumanMessage, AIMessage]
    assert history[-1].content == FINAL_TEXT


def test_create_agent_demo_openai_mock_transport(mock_provider, fixture_execution):
    run, requests = mock_provider
    call = _tool_call()

    (history,) = run([_tool_reply(call), _final_reply()])

    first, second = requests
    fixture_execution.assert_awaited_once_with(component="task_service")
    assert [message["role"] for message in first["messages"]] == ["system", "user"]
    assert "synthetic training fixtures" in first["messages"][0]["content"]
    assert json.loads(first["messages"][1]["content"]) == {
        "goal": GOAL, "acceptance_criteria": list(CRITERIA)
    }
    (tool_definition,) = first["tools"]
    assert tool_definition["type"] == "function"
    assert tool_definition["function"]["name"] == "read_fixture"
    schema = tool_definition["function"]["parameters"]
    assert schema["required"] == ["component"]
    assert set(schema["properties"]) == {"component"}
    assert set(schema["properties"]["component"]["enum"]) == {"task_service", "agent_run_service"}

    assert [message["role"] for message in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][:2] == first["messages"]
    (assistant_call,) = second["messages"][2]["tool_calls"]
    assert assistant_call["id"] == call["id"]
    assert assistant_call["type"] == "function"
    assert assistant_call["function"]["name"] == "read_fixture"
    assert json.loads(assistant_call["function"]["arguments"]) == {"component": "task_service"}
    assert second["messages"][3] == {
        "role": "tool", "tool_call_id": call["id"], "content": FIXTURE_TEXT
    }

    assert [type(message) for message in history] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert history[2].tool_call_id == call["id"]
    assert history[2].status == "success"
    assert history[2].content == FIXTURE_TEXT
    assert history[3].content == FINAL_TEXT
    for reply, finish_reason, tokens in (
        (history[1], "tool_calls", (12, 8, 20)),
        (history[3], "stop", (24, 8, 32)),
    ):
        assert reply.response_metadata["finish_reason"] == finish_reason
        usage = reply.usage_metadata
        assert usage is not None
        assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == tokens


@pytest.mark.parametrize("batch", [False, True], ids=["repeat", "batch"])
def test_create_agent_demo_tool_budget(mock_provider, fixture_execution, batch):
    run, requests = mock_provider
    first, second = _tool_call(), _tool_call("fixture-2", component="agent_run_service")
    replies = [_tool_reply(first, second)] if batch else [_tool_reply(first), _tool_reply(second)]

    with pytest.raises(ToolCallLimitExceededError):
        run(replies)

    if batch:
        assert len(requests) == 1
        fixture_execution.assert_not_awaited()
    else:
        assert len(requests) == 2
        fixture_execution.assert_awaited_once_with(component="task_service")


def test_create_agent_demo_invalid_arguments_produce_error_observation(mock_provider, fixture_execution):
    run, requests = mock_provider
    call = _tool_call(component="unknown")

    (history,) = run([_tool_reply(call), _final_reply()])

    assert len(requests) == 2
    fixture_execution.assert_not_awaited()
    error = history[2]
    assert isinstance(error, ToolMessage)
    assert error.status == "error"
    assert error.tool_call_id == call["id"]
    assert requests[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": call["id"], "content": error.content
    }


def test_create_agent_demo_model_budget(mock_provider, fixture_execution):
    run, requests = mock_provider

    def model_budget_one(model):
        return create_agent(
            model=model,
            tools=[read_fixture],
            middleware=[
                ModelCallLimitMiddleware(run_limit=1, exit_behavior="error"),
                ToolCallLimitMiddleware(run_limit=1, exit_behavior="error"),
            ],
            checkpointer=None,
        )

    with pytest.raises(ModelCallLimitExceededError):
        run([_tool_reply(_tool_call())], factory=model_budget_one)

    assert len(requests) == 1
    fixture_execution.assert_awaited_once_with(component="task_service")


def test_create_agent_demo_run_limits_reset_for_same_agent(mock_provider, fixture_execution):
    run, requests = mock_provider
    replies = [
        _tool_reply(_tool_call("fixture-1")),
        _final_reply(),
        _tool_reply(_tool_call("fixture-2")),
        _final_reply(),
    ]

    first, second = run(replies, runs=2)

    assert len(requests) == 4
    assert fixture_execution.await_count == 2
    assert [first[2].tool_call_id, second[2].tool_call_id] == ["fixture-1", "fixture-2"]
    assert first[-1].content == second[-1].content == FINAL_TEXT


def test_create_agent_demo_propagates_provider_timeout(mock_provider, fixture_execution):
    run, requests = mock_provider
    timeout = httpx.ReadTimeout("synthetic timeout")

    with pytest.raises(OpenAITimeoutError) as exc:
        run([timeout])

    sdk_error = exc.value.__cause__
    assert isinstance(sdk_error, APITimeoutError)
    assert sdk_error.__cause__ is timeout
    assert len(requests) == 1
    fixture_execution.assert_not_awaited()


@pytest.mark.parametrize(
    "state,reason",
    [
        (None, "invalid_state"),
        ({}, "invalid_messages"),
        ({"messages": []}, "invalid_messages"),
        ({"messages": [None, AIMessage(content="done")]}, "invalid_messages"),
        ({"messages": [HumanMessage(content="no final reply")]}, "invalid_final_message"),
        (
            {"messages": [AIMessage(content="", tool_calls=[
                {"name": "read_fixture", "args": {"component": "task_service"}, "id": "fixture-1"}
            ])]},
            "unfinished_tool_request",
        ),
        (
            {"messages": [AIMessage(content="", invalid_tool_calls=[
                {"name": "read_fixture", "args": "{", "id": "fixture-1"}
            ])]},
            "unfinished_tool_request",
        ),
    ],
    ids=["state", "missing", "empty", "history-item", "final-type", "pending-tool", "malformed-tool"],
)
def test_create_agent_demo_rejects_invalid_state(state, reason):
    agent = create_autospec(Runnable, instance=True)
    agent.ainvoke.return_value = state

    with pytest.raises(CreateAgentDemoError, match=f"^{reason}$"):
        asyncio.run(run_create_agent_demo(agent, goal=GOAL, acceptance_criteria=CRITERIA))

    human = build_task_messages(GOAL, CRITERIA)[1]
    agent.ainvoke.assert_awaited_once_with({"messages": [human]}, config={"recursion_limit": 16})
