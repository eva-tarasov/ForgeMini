import asyncio
import json
import os
import socket
import subprocess
import sys
from unittest.mock import AsyncMock, create_autospec, patch

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tracers.langchain import LangChainTracer
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from pydantic import ValidationError

from app.agent_runtime import model as model_layer
from app.agent_runtime.settings import ModelSettings
from app.agent_runtime.structured_output import (
    StructuredOutputError,
    TaskAnalysis,
    TaskAnalysisResult,
    create_structured_task_model,
    invoke_structured_task_model,
)
from app.agent_runtime.tool_calling_demo import (
    FixtureInput,
    ToolCallingDemoError,
    create_tool_calling_model,
    read_fixture,
    run_tool_calling_demo,
)


TRACING_OFF = dict.fromkeys(
    (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_HANDLER",
    ),
    "false",
)


@pytest.fixture(autouse=True)
def offline_model_tests(monkeypatch, request):
    for name, value in getattr(request, "param", {}).items():
        monkeypatch.setenv(name, value)
    for name, value in TRACING_OFF.items():
        monkeypatch.setenv(name, value)

    def reject_network(*args, **kwargs):
        pytest.fail("Model tests must not connect to the network")

    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)

    # LangSmith can cache environment lookups before this fixture runs.
    with tracing_context(enabled=False, parent=False):
        yield


@pytest.mark.parametrize(
    "offline_model_tests",
    [
        {
            **dict.fromkeys(TRACING_OFF, "true"),
            "LANGSMITH_API_KEY": "synthetic-langsmith-key",
            "LANGCHAIN_API_KEY": "synthetic-langchain-key",
            "OPENAI_API_KEY": "synthetic-openai-key",
        },
    ],
    indirect=True,
    ids=["tracing-enabled-in-environment"],
)
def test_fake_model_does_not_create_tracer_with_local_tracing_enabled():
    assert all(os.environ[name] == "false" for name in TRACING_OFF)
    model = GenericFakeChatModel(messages=iter([AIMessage(content="offline reply")]))

    with patch.object(LangChainTracer, "__init__", side_effect=AssertionError("Unexpected tracer")) as tracer_init:
        result = asyncio.run(model_layer.invoke_task_model(model, goal="Цель", acceptance_criteria=()))

    assert result.content == "offline reply"
    tracer_init.assert_not_called()


def test_build_task_messages_preserves_unicode_and_original_values():
    goal = '  Проверить «задачу»\nс переносом  '
    criteria = ("2. Сохранить порядок", '1. Сохранить "кавычки"', "  Без обрезки  ")

    system, human = model_layer.build_task_messages(goal, criteria)

    assert isinstance(system, SystemMessage)
    assert "Analyze the supplied task data" in system.content
    assert "Do not claim you read files, changed code, or ran or passed tests" in system.content
    assert isinstance(human, HumanMessage)
    assert "Проверить «задачу»" in human.content
    assert json.loads(human.content) == {"goal": goal, "acceptance_criteria": list(criteria)}


def test_invoke_calls_injected_model_once_and_returns_its_message():
    model = create_autospec(BaseChatModel, instance=True)
    reply = AIMessage(content="No usage was supplied")
    model.ainvoke.return_value = reply

    result = asyncio.run(
        model_layer.invoke_task_model(model, goal="Цель", acceptance_criteria=("Критерий",))
    )

    model.ainvoke.assert_awaited_once_with(model_layer.build_task_messages("Цель", ("Критерий",)))
    assert result is reply
    assert result.usage_metadata is None


def test_invoke_with_langchain_fake_preserves_metadata_and_tool_calls():
    reply = AIMessage(
        content="Результат",
        response_metadata={"finish_reason": "tool_calls", "model_name": "fake-model"},
        usage_metadata={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
        tool_calls=[{"name": "read_fixture", "args": {}, "id": "call_fixture"}],
    )
    model = GenericFakeChatModel(messages=iter([reply]))

    result = asyncio.run(model_layer.invoke_task_model(model, goal="Цель", acceptance_criteria=()))

    assert isinstance(result, AIMessage)
    assert result.content == "Результат"
    assert result.response_metadata == {"finish_reason": "tool_calls", "model_name": "fake-model"}
    assert result.usage_metadata == {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
    assert result.tool_calls == [
        {"name": "read_fixture", "args": {}, "id": "call_fixture", "type": "tool_call"}
    ]


def test_invoke_propagates_model_error():
    model = create_autospec(BaseChatModel, instance=True)
    error = TimeoutError("synthetic model timeout")
    model.ainvoke.side_effect = error

    with pytest.raises(TimeoutError) as exc_info:
        asyncio.run(model_layer.invoke_task_model(model, goal="Цель", acceptance_criteria=()))

    assert exc_info.value is error
    model.ainvoke.assert_awaited_once()


def test_factory_passes_settings_to_chat_openai(monkeypatch):
    constructor = create_autospec(model_layer.ChatOpenAI)
    monkeypatch.setattr(model_layer, "ChatOpenAI", constructor)
    settings = ModelSettings(
        _env_file=None,
        openai_api_key="synthetic-key",
        forge_model_name="synthetic-model",
        forge_model_timeout_seconds=8.5,
    )

    assert model_layer.create_openai_model(settings) is constructor.return_value
    constructor.assert_called_once_with(
        model="synthetic-model", api_key=settings.openai_api_key, timeout=8.5, max_retries=0
    )


def test_model_import_and_backend_start_without_openai_settings(tmp_path):
    # A fresh process tests imports before any model configuration is loaded.
    script = """
import socket
import sys
from pathlib import Path

def reject_network(*args, **kwargs):
    raise AssertionError("Model import and backend startup must not connect to the network")

socket.getaddrinfo = reject_network
socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network

from fastapi.testclient import TestClient
from app.core import settings as core_settings

core_settings.ROOT_ENV = Path(sys.argv[1])

import app.agent_runtime.model
from app.main import create_app

db_settings = core_settings.Settings(
    _env_file=None,
    postgres_user="test_user",
    postgres_password="test_password",
    postgres_db="test_db",
)
with TestClient(create_app(settings=db_settings)) as client:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
"""
    subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path / ".env")],
        env=TRACING_OFF,
        check=True,
        timeout=15,
    )


@pytest.fixture
def analysis_payload():
    return {
        "summary": "Проверить границу model layer",
        "relevant_components": ["agent_runtime"],
        "assumptions": ["Предоставленных данных достаточно для предварительного анализа"],
        "blocked_reason": None,
    }


def test_task_analysis_accepts_valid_data_and_nullable_blocked_reason(analysis_payload):
    assert TaskAnalysis.model_validate(analysis_payload).model_dump() == analysis_payload
    analysis_payload["blocked_reason"] = "Нужны дополнительные данные"
    assert TaskAnalysis.model_validate(analysis_payload).blocked_reason == "Нужны дополнительные данные"


@pytest.mark.parametrize(
    "field,invalid_value",
    [
        ("summary", 42),
        ("relevant_components", "agent_runtime"),
        ("assumptions", [42]),
        ("blocked_reason", 42),
        ("extra_field", "unexpected"),
    ],
)
def test_task_analysis_rejects_invalid_schema(analysis_payload, field, invalid_value):
    with pytest.raises(ValidationError) as exc:
        TaskAnalysis.model_validate({**analysis_payload, field: invalid_value})

    assert exc.value.errors()[0]["loc"][0] == field


@pytest.mark.parametrize("field", ["summary", "relevant_components", "assumptions", "blocked_reason"])
def test_task_analysis_requires_every_field(analysis_payload, field):
    del analysis_payload[field]

    with pytest.raises(ValidationError) as exc:
        TaskAnalysis.model_validate(analysis_payload)

    assert exc.value.errors()[0]["loc"] == (field,)
    assert exc.value.errors()[0]["type"] == "missing"


def test_task_analysis_exports_required_fields_and_forbids_extra_keys(analysis_payload):
    schema = TaskAnalysis.model_json_schema()

    assert set(schema["properties"]) == set(schema["required"]) == set(analysis_payload)
    assert schema["additionalProperties"] is False


def test_structured_model_factory_wraps_existing_client():
    model = create_autospec(ChatOpenAI, instance=True)

    assert create_structured_task_model(model) is model.with_structured_output.return_value
    model.with_structured_output.assert_called_once_with(
        TaskAnalysis, method="json_schema", strict=True, include_raw=True
    )
    model.ainvoke.assert_not_awaited()
    model.invoke.assert_not_called()


@pytest.mark.parametrize(
    "usage",
    [None, {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}],
    ids=["no-usage", "with-usage"],
)
def test_structured_output_returns_original_parsed_and_raw(analysis_payload, usage):
    raw = AIMessage(
        content="synthetic raw content",
        response_metadata={"finish_reason": "stop"},
        usage_metadata=usage,
    )
    parsed = TaskAnalysis.model_validate(analysis_payload)
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = {"raw": raw, "parsed": parsed, "parsing_error": None}

    result = asyncio.run(
        invoke_structured_task_model(model, goal="Цель", acceptance_criteria=("Критерий",))
    )

    model.ainvoke.assert_awaited_once_with(
        model_layer.build_task_messages("Цель", ("Критерий",))
    )
    assert isinstance(result, TaskAnalysisResult)
    assert result.analysis is parsed
    assert result.raw is raw
    assert result.raw.usage_metadata == usage
    assert result.raw.response_metadata == {"finish_reason": "stop"}
    assert "synthetic raw content" not in repr(result)


def test_structured_output_rejects_parsing_error(analysis_payload):
    raw = AIMessage(content="synthetic raw content")
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = {
        "raw": raw,
        "parsed": TaskAnalysis.model_validate(analysis_payload),
        "parsing_error": ValueError("synthetic parser detail"),
    }

    with pytest.raises(StructuredOutputError) as exc:
        asyncio.run(invoke_structured_task_model(model, goal="Цель", acceptance_criteria=()))

    assert exc.value.reason == str(exc.value) == "parsing_failed"
    assert exc.value.raw is raw
    assert exc.value.__cause__ is None
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize(
    "invalid_envelope",
    [
        [],
        {"parsed": None, "parsing_error": None},
        {"raw": None, "parsing_error": None},
        {"raw": None, "parsed": None},
    ],
    ids=["non-mapping", "no-raw-key", "no-parsed-key", "no-parsing-error-key"],
)
def test_structured_output_rejects_invalid_envelope(invalid_envelope):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = invalid_envelope

    with pytest.raises(StructuredOutputError, match="^invalid_envelope$") as exc:
        asyncio.run(invoke_structured_task_model(model, goal="Цель", acceptance_criteria=()))

    assert exc.value.raw is None


@pytest.mark.parametrize(
    "field,invalid_value,reason",
    [
        ("raw", HumanMessage(content="not an AI message"), "invalid_raw"),
        ("parsed", None, "missing_parsed"),
        ("parsed", {"summary": "unvalidated dict"}, "invalid_parsed"),
    ],
)
def test_structured_output_rejects_invalid_envelope_values(field, invalid_value, reason):
    raw = AIMessage(content="synthetic raw content")
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = {
        "raw": raw,
        "parsed": None,
        "parsing_error": None,
        field: invalid_value,
    }

    with pytest.raises(StructuredOutputError, match=f"^{reason}$") as exc:
        asyncio.run(invoke_structured_task_model(model, goal="Цель", acceptance_criteria=()))

    assert exc.value.raw is (None if field == "raw" else raw)


def test_structured_output_propagates_model_exception():
    model = create_autospec(Runnable, instance=True)
    error = TimeoutError("synthetic model timeout")
    model.ainvoke.side_effect = error

    with pytest.raises(TimeoutError) as exc:
        asyncio.run(invoke_structured_task_model(model, goal="Цель", acceptance_criteria=()))

    assert exc.value is error
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("refusal", [False, True], ids=["valid-output", "refusal"])
def test_structured_output_openai_mock_transport(analysis_payload, refusal):
    requests = []
    refusal_text = "Synthetic refusal"

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-forge-test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": None if refusal else json.dumps(analysis_payload),
                            "refusal": refusal_text if refusal else None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
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
                return await invoke_structured_task_model(
                    create_structured_task_model(model),
                    goal="Цель",
                    acceptance_criteria=("Критерий",),
                )

    if refusal:
        with pytest.raises(StructuredOutputError, match="^parsing_failed$") as exc:
            asyncio.run(invoke())
        raw = exc.value.raw
        assert isinstance(raw, AIMessage)
        assert raw.additional_kwargs["refusal"] == refusal_text
    else:
        result = asyncio.run(invoke())
        assert isinstance(result.analysis, TaskAnalysis)
        assert result.analysis.model_dump() == analysis_payload
        raw = result.raw
        assert json.loads(raw.content) == analysis_payload

    assert raw.response_metadata["finish_reason"] == "stop"
    usage = raw.usage_metadata
    assert usage is not None
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (12, 8, 20)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert json.loads(body["messages"][1]["content"]) == {
        "goal": "Цель",
        "acceptance_criteria": ["Критерий"],
    }
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert set(schema["properties"]) == set(schema["required"]) == set(analysis_payload)
    assert schema["additionalProperties"] is False


TASK_SERVICE_FIXTURE = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)


def _fixture_call(name="read_fixture", call_id="call_fixture", **args):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _run_tool_demo(model):
    return asyncio.run(run_tool_calling_demo(model, goal="Цель", acceptance_criteria=("Критерий",)))


@pytest.fixture
def fixture_execution(monkeypatch):
    execution = AsyncMock(wraps=read_fixture.coroutine)
    monkeypatch.setattr(read_fixture, "coroutine", execution)
    return execution


def test_fixture_input_schema():
    schema = FixtureInput.model_json_schema()

    assert schema["required"] == ["component"]
    assert set(schema["properties"]) == {"component"}
    assert set(schema["properties"]["component"]["enum"]) == {"task_service", "agent_run_service"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "component,expected",
    [
        ("task_service", TASK_SERVICE_FIXTURE),
        (
            "agent_run_service",
            "Training fixture (synthetic): a transition checks the expected current status "
            "against the stored status before changing it.",
        ),
    ],
)
def test_read_fixture_returns_training_data(component, expected):
    assert asyncio.run(read_fixture.ainvoke({"component": component})) == expected


def test_tool_calling_model_binds_the_existing_client_without_inference():
    model = create_autospec(ChatOpenAI, instance=True)

    assert create_tool_calling_model(model) is model.bind_tools.return_value
    model.bind_tools.assert_called_once_with([read_fixture], parallel_tool_calls=False)
    model.ainvoke.assert_not_awaited()
    model.invoke.assert_not_called()


def test_tool_calling_demo_returns_after_first_reply_without_tool_calls(fixture_execution):
    model = create_autospec(Runnable, instance=True)
    first = AIMessage(content="No fixture needed")
    model.ainvoke.return_value = first

    history = _run_tool_demo(model)

    assert [type(message) for message in history] == [SystemMessage, HumanMessage, AIMessage]
    assert history[-1] is first
    assert "predefined training fixtures" in history[0].content
    assert history[1] == model_layer.build_task_messages("Цель", ("Критерий",))[1]
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()


def test_tool_calling_demo_returns_both_replies_and_correlated_fixture(fixture_execution):
    first = AIMessage(
        content="",
        tool_calls=[_fixture_call(component="task_service")],
        usage_metadata={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
    )
    final = AIMessage(content="Fixture considered", response_metadata={"finish_reason": "stop"})
    replies = iter([first, final])
    snapshots = []

    async def respond(messages):
        snapshots.append(list(messages))
        return next(replies)

    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = respond

    history = _run_tool_demo(model)

    assert [type(message) for message in history] == [
        SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessage
    ]
    assert snapshots == [history[:2], history[:4]]
    assert history[2] is first
    assert history[4] is final
    assert history[3].tool_call_id == "call_fixture"
    assert history[3].status == "success"
    assert history[3].content == TASK_SERVICE_FIXTURE
    assert model.ainvoke.await_count == 2
    fixture_execution.assert_awaited_once_with(component="task_service")


@pytest.mark.parametrize(
    "reply,reason",
    [
        (HumanMessage(content="unexpected message type"), "invalid_model_message"),
        (
            AIMessage(content="", invalid_tool_calls=[{"args": "{", "name": "read_fixture"}]),
            "invalid_tool_call",
        ),
        (AIMessage(content="", tool_calls=[_fixture_call(name="unknown")]), "unknown_tool"),
        (AIMessage(content="", tool_calls=[_fixture_call(call_id=None)]), "invalid_tool_call_id"),
        (AIMessage(content="", tool_calls=[_fixture_call(call_id=" ")]), "invalid_tool_call_id"),
        (
            AIMessage(
                content="",
                tool_calls=[
                    _fixture_call(component="task_service"),
                    _fixture_call(call_id="call_other", component="agent_run_service"),
                ],
            ),
            "tool_budget_exceeded",
        ),
    ],
    ids=["wrong-message", "invalid-tool-call", "unknown-tool", "missing-id", "blank-id", "batch"],
)
def test_tool_calling_demo_rejects_invalid_first_reply(reply, reason, fixture_execution):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = reply

    with pytest.raises(ToolCallingDemoError, match=f"^{reason}$"):
        _run_tool_demo(model)

    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()


@pytest.mark.parametrize(
    "args,error_type",
    [
        ({"component": "unknown"}, "literal_error"),
        ({"component": "task_service", "extra": "unexpected"}, "extra_forbidden"),
    ],
    ids=["unknown-component", "extra-argument"],
)
def test_tool_calling_demo_rejects_invalid_fixture(args, error_type, fixture_execution):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = AIMessage(content="", tool_calls=[_fixture_call(**args)])

    with pytest.raises(ValidationError) as exc:
        _run_tool_demo(model)

    assert exc.value.errors(include_input=False)[0]["type"] == error_type
    model.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()


def test_tool_calling_demo_rejects_second_tool_call(fixture_execution):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = [
        AIMessage(content="", tool_calls=[_fixture_call(component="task_service")]),
        AIMessage(
            content="",
            tool_calls=[_fixture_call(call_id="call_other", component="agent_run_service")],
        ),
    ]

    with pytest.raises(ToolCallingDemoError, match="^tool_budget_exceeded$"):
        _run_tool_demo(model)

    assert model.ainvoke.await_count == 2
    fixture_execution.assert_awaited_once_with(component="task_service")


@pytest.mark.parametrize("call_number", [1, 2])
def test_tool_calling_demo_propagates_provider_timeout(call_number, fixture_execution):
    model = create_autospec(Runnable, instance=True)
    error = TimeoutError("synthetic provider timeout")
    first = AIMessage(content="", tool_calls=[_fixture_call(component="task_service")])
    model.ainvoke.side_effect = [error] if call_number == 1 else [first, error]

    with pytest.raises(TimeoutError) as exc:
        _run_tool_demo(model)

    assert exc.value is error
    assert model.ainvoke.await_count == call_number
    assert fixture_execution.await_count == call_number - 1


@pytest.mark.parametrize(
    "tool_reply,reason",
    [
        ("unexpected result", "invalid_tool_message"),
        (ToolMessage(content="fixture", tool_call_id="other"), "tool_call_id_mismatch"),
        (
            ToolMessage(content="failure", tool_call_id="call_fixture", status="error"),
            "tool_execution_failed",
        ),
    ],
    ids=["wrong-message", "wrong-id", "failed-status"],
)
def test_tool_calling_demo_rejects_invalid_tool_result(tool_reply, reason):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.return_value = AIMessage(content="", tool_calls=[_fixture_call(component="task_service")])

    with patch.object(type(read_fixture), "ainvoke", return_value=tool_reply) as invoke_tool:
        with pytest.raises(ToolCallingDemoError, match=f"^{reason}$"):
            _run_tool_demo(model)

    invoke_tool.assert_awaited_once_with(_fixture_call(component="task_service"))
    model.ainvoke.assert_awaited_once()


def test_tool_calling_demo_openai_mock_transport(fixture_execution):
    call_id = "call_training_fixture"
    arguments = {"component": "task_service"}
    requests = []
    replies = iter(
        [
            (
                "tool_calls",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "read_fixture", "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            ),
            (
                "stop",
                {"role": "assistant", "content": "The training fixture scopes the key to a workspace."},
                {"prompt_tokens": 24, "completion_tokens": 10, "total_tokens": 34},
            ),
        ]
    )

    def respond(request):
        assert request.method == "POST"
        assert request.url == httpx.URL("https://forge-test.invalid/v1/chat/completions")
        requests.append(json.loads(request.content))
        finish_reason, message, usage = next(replies)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-fixture-{len(requests)}",
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
                return await run_tool_calling_demo(
                    create_tool_calling_model(model),
                    goal="Цель",
                    acceptance_criteria=("Критерий",),
                )

    history = asyncio.run(invoke())

    assert len(requests) == 2
    fixture_execution.assert_awaited_once_with(component="task_service")
    assert [type(message) for message in history] == [
        SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessage
    ]
    first, second = requests
    assert [message["role"] for message in first["messages"]] == ["system", "user"]
    assert "predefined training fixtures" in first["messages"][0]["content"]
    assert json.loads(first["messages"][1]["content"]) == {
        "goal": "Цель",
        "acceptance_criteria": ["Критерий"],
    }
    assert first["parallel_tool_calls"] is False
    (tool_definition,) = first["tools"]
    assert tool_definition["type"] == "function"
    assert tool_definition["function"]["name"] == "read_fixture"
    parameters = tool_definition["function"]["parameters"]
    assert parameters["required"] == ["component"]
    assert set(parameters["properties"]) == {"component"}
    assert set(parameters["properties"]["component"]["enum"]) == {"task_service", "agent_run_service"}

    assert [message["role"] for message in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][:2] == first["messages"]
    (assistant_call,) = second["messages"][2]["tool_calls"]
    assert assistant_call["id"] == call_id
    assert assistant_call["type"] == "function"
    assert assistant_call["function"]["name"] == "read_fixture"
    assert json.loads(assistant_call["function"]["arguments"]) == arguments
    assert second["messages"][3] == {
        "role": "tool", "tool_call_id": call_id, "content": TASK_SERVICE_FIXTURE
    }

    assert history[2].tool_calls == [_fixture_call(call_id=call_id, **arguments)]
    assert history[3].tool_call_id == call_id
    assert history[4].content == "The training fixture scopes the key to a workspace."
    assert history[4].tool_calls == []
    for message, finish_reason, expected_usage in (
        (history[2], "tool_calls", (12, 8, 20)),
        (history[4], "stop", (24, 10, 34)),
    ):
        assert message.response_metadata["finish_reason"] == finish_reason
        usage = message.usage_metadata
        assert usage is not None
        assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == expected_usage
