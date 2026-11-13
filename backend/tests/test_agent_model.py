import asyncio
import json
import os
import socket
import subprocess
import sys
from unittest.mock import create_autospec, patch

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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
