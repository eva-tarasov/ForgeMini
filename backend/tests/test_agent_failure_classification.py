import asyncio
from functools import wraps
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from model_test_support import offline_model_tests

from app.agent_runtime.agent_graph import AgentGraphError, create_agent_graph
from app.agent_runtime.error_classification import classify_runtime_error
from app.agent_runtime.finalization import FinalizationError
from app.agent_runtime.fixture_tools import create_tool_calling_model, read_fixture
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.structured_output import StructuredOutputError
from app.services.agent_failure import AgentFailureCode
from app.services.agent_runner import AgentRunnerError, AgentRunRequest


URL = "https://forge-test.invalid/v1/chat/completions"
REQUEST = httpx.Request("POST", URL)


@pytest.mark.parametrize("status,provider_code,expected", [
    (400, None, "provider_request_rejected"),
    (401, None, "provider_auth"),
    (403, None, "provider_auth"),
    (404, None, "provider_request_rejected"),
    (408, None, "provider_timeout"),
    (409, None, "provider_error"),
    (422, None, "provider_request_rejected"),
    (429, "insufficient_quota", "provider_quota_exhausted"),
    (429, "rate_limit_exceeded", "provider_rate_limited"),
    (429, "synthetic unknown", "provider_rate_limit_unknown"),
    (429, None, "provider_rate_limit_unknown"),
    (500, None, "provider_unavailable"),
    (599, None, "provider_unavailable"),
])
def test_runtime_classifies_sdk_status_error(status, provider_code, expected):
    error = openai.APIStatusError(
        "synthetic error", response=httpx.Response(status, request=REQUEST), body={"code": provider_code},
    )
    assert classify_runtime_error(error) is AgentFailureCode(expected)


@pytest.mark.parametrize("error,expected", [
    (openai.APITimeoutError(REQUEST), "provider_timeout"),
    (openai.APIConnectionError(request=REQUEST), "provider_connection"),
    (openai.APIError("synthetic error", REQUEST, body=None), "provider_error"),
    (GraphRecursionError("synthetic error"), "graph_budget_exceeded"),
    (AgentGraphError("model_budget_exceeded"), "graph_budget_exceeded"),
    (AgentGraphError("tool_budget_exceeded"), "graph_budget_exceeded"),
    (AgentGraphError("finalization_budget_exceeded"), "graph_budget_exceeded"),
    (AgentGraphError("synthetic model_budget_exceeded"), "graph_execution_failed"),
    (AgentGraphError("empty_model_response"), "graph_execution_failed"),
    (StructuredOutputError("synthetic parsing error", raw=AIMessage(content="private synthetic output")),
     "structured_output_invalid"),
    (FinalizationError("synthetic error"), "structured_output_invalid"),
    (TimeoutError("synthetic error"), "runner_timeout"),
    (RuntimeError("provider_timeout"), "execution_failed"),
])
def test_runtime_classifies_exception_type_and_controlled_graph_reason(error, expected):
    assert classify_runtime_error(error) is AgentFailureCode(expected)


@pytest.mark.parametrize("status,code,sdk_error,expected", [
    (401, None, openai.AuthenticationError, "provider_auth"),
    (429, "rate_limit_exceeded", openai.RateLimitError, "provider_rate_limited"),
    (429, "insufficient_quota", openai.RateLimitError, "provider_quota_exhausted"),
    (429, "synthetic_unknown", openai.RateLimitError, "provider_rate_limit_unknown"),
    (503, None, openai.InternalServerError, "provider_unavailable"),
])
def test_langgraph_runner_classifies_openai_http_error(monkeypatch, status, code, sdk_error, expected):
    requests = []
    finalizer = create_autospec(Runnable, instance=True)
    original_tool = read_fixture.coroutine
    tool_call = AsyncMock(wraps=original_tool)

    @wraps(original_tool)
    async def observe_fixture(**kwargs):
        return await tool_call(**kwargs)

    monkeypatch.setattr(read_fixture, "coroutine", observe_fixture)

    def respond(request):
        assert request.method == "POST"
        assert request.url == httpx.URL(URL)
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic private detail", "code": code}})

    async def invoke():
        transport = httpx.MockTransport(respond)
        with httpx.Client(transport=transport, trust_env=False) as sync_client:
            async with httpx.AsyncClient(transport=transport, trust_env=False) as async_client:
                model = ChatOpenAI(
                    model="gpt-4o-mini", api_key="synthetic-openai-key",
                    base_url="https://forge-test.invalid/v1", openai_proxy="",
                    use_responses_api=False, max_retries=0,
                    http_client=sync_client, http_async_client=async_client,
                )
                graph = create_agent_graph(create_tool_calling_model(model), finalizer)
                runner = LangGraphAgentRunner(graph)
                async with asyncio.timeout(5):
                    await runner.run(AgentRunRequest(uuid4(), uuid4(), "Synthetic task", ("One criterion",)))

    with pytest.raises(AgentRunnerError, match=f"^{expected}$") as exc:
        asyncio.run(invoke())
    assert exc.value.reason == expected
    runtime_error = exc.value.__cause__
    assert isinstance(runtime_error, sdk_error)
    assert runtime_error.status_code == status
    # ChatOpenAI wraps the genuine SDK exception in a compatible subclass.
    assert type(runtime_error.__cause__) is sdk_error
    assert runtime_error.__cause__.status_code == status
    assert len(requests) == 1
    tool_call.assert_not_awaited()
    finalizer.ainvoke.assert_not_awaited()
