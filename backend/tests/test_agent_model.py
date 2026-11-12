import asyncio
import json
import os
import socket
import subprocess
import sys
from unittest.mock import create_autospec, patch

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import tracing_context

from app.agent_runtime import model as model_layer
from app.agent_runtime.settings import ModelSettings


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
