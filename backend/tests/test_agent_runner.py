import asyncio
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from functools import wraps
from unittest.mock import AsyncMock, create_autospec
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from model_test_support import TRACING_OFF, offline_model_tests
from pydantic import ValidationError

from app.agent_runtime import agent_result as runtime_result
from app.agent_runtime.agent_graph import AgentGraphError, create_agent_graph
from app.agent_runtime.fixture_tools import read_fixture
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.structured_output import TaskAnalysis
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_runner import AgentRunner, AgentRunnerError, AgentRunRequest


TASK_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("00000000-0000-4000-8000-000000000002")
GOAL = "Проверить область действия ключа"
CRITERIA = ("Отделить fixture от допущений",)
FIXTURE_TEXT = (
    "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
    "The same key may be used in a different workspace."
)


def _request(goal=GOAL, *, run_id=RUN_ID):
    return AgentRunRequest(task_id=TASK_ID, run_id=run_id, goal=goal, acceptance_criteria=CRITERIA)


def _result(**overrides):
    return AgentFinalResult(**{
        "summary": "Предварительный анализ",
        "status": "ready_for_verification",
        "relevant_components": (),
        "assumptions": (),
        "observations": (),
        "blocked_reason": None,
        **overrides,
    })


def _fixture_observation(**overrides):
    return FixtureObservation(**{
        "component": "task_service",
        "tool_call_id": "fixture-1",
        "content": FIXTURE_TEXT,
        **overrides,
    })


def _model(*replies):
    model = create_autospec(Runnable, instance=True)
    model.ainvoke.side_effect = list(replies)
    return model


def _analysis_reply(summary="Предварительный анализ", blocked_reason=None):
    return {
        "raw": AIMessage(content=""),
        "parsed": TaskAnalysis(
            summary=summary, relevant_components=[], assumptions=[], blocked_reason=blocked_reason
        ),
        "parsing_error": None,
    }


def _tool_reply():
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_fixture", "id": "fixture-1", "args": {"component": "task_service"}}],
    )


@pytest.fixture
def fixture_execution(monkeypatch):
    original = read_fixture.coroutine
    execution = AsyncMock(wraps=original)

    @wraps(original)
    async def observed(**kwargs):
        return await execution(**kwargs)

    monkeypatch.setattr(read_fixture, "coroutine", observed)
    return execution


def test_agent_run_request_is_a_frozen_snapshot():
    request = _request()

    assert request.task_id is TASK_ID
    assert request.run_id is RUN_ID
    assert request.goal == GOAL
    assert isinstance(request.acceptance_criteria, tuple)
    assert request.acceptance_criteria == CRITERIA
    with pytest.raises(FrozenInstanceError):
        request.goal = "changed"


def test_agent_runner_port_accepts_an_independent_fake():
    class FakeRunner:
        async def run(self, request: AgentRunRequest) -> AgentFinalResult:
            return _result(summary=request.goal)

    runner: AgentRunner = FakeRunner()

    assert asyncio.run(runner.run(_request())) == _result(summary=GOAL)


def test_runtime_result_imports_reexport_the_application_classes():
    assert runtime_result.AgentFinalResult is AgentFinalResult
    assert runtime_result.FixtureObservation is FixtureObservation


def test_agent_result_preserves_serialization_and_frozen_values():
    observation = _fixture_observation(tool_call_id=" fixture-1 ", content="  Синтетический ответ  ")
    result = _result(
        relevant_components=("task_service",), assumptions=("Допущение",), observations=(observation,)
    )

    assert json.loads(result.model_dump_json()) == {
        "summary": "Предварительный анализ",
        "status": "ready_for_verification",
        "relevant_components": ["task_service"],
        "assumptions": ["Допущение"],
        "observations": [{
            "source": "synthetic_fixture",
            "tool_name": "read_fixture",
            "component": "task_service",
            "tool_call_id": " fixture-1 ",
            "content": "  Синтетический ответ  ",
        }],
        "blocked_reason": None,
    }
    assert AgentFinalResult.model_validate_json(result.model_dump_json()) == result
    assert isinstance(result.observations, tuple)
    with pytest.raises(ValidationError) as result_error:
        result.summary = "changed"
    assert result_error.value.errors()[0]["type"] == "frozen_instance"
    with pytest.raises(ValidationError) as observation_error:
        result.observations[0].content = "changed"
    assert observation_error.value.errors()[0]["type"] == "frozen_instance"


@pytest.mark.parametrize(
    "status,reason",
    [("ready_for_verification", None), ("blocked", " Нужны данные ")],
)
def test_agent_result_accepts_valid_status_and_empty_collections(status, reason):
    result = _result(status=status, blocked_reason=reason)

    assert (result.status, result.blocked_reason) == (status, reason)
    assert result.relevant_components == result.assumptions == result.observations == ()


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"blocked_reason": "Reason"}, "unexpected_blocked_reason"),
        ({"status": "blocked"}, "missing_blocked_reason"),
        ({"status": "blocked", "blocked_reason": " \t"}, "missing_blocked_reason"),
        ({"summary": ""}, "empty_summary"),
        ({"summary": " \n"}, "empty_summary"),
        ({"relevant_components": ("",)}, "empty_analysis_item"),
        ({"assumptions": (" ",)}, "empty_analysis_item"),
    ],
    ids=["ready-with-reason", "blocked-with-none", "blocked-with-whitespace", "empty-summary",
         "blank-summary", "empty-component", "blank-assumption"],
)
def test_agent_result_rejects_invalid_values(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _result(**overrides)


@pytest.mark.parametrize("field", ["tool_call_id", "content"])
@pytest.mark.parametrize("value", ["", " \t"])
def test_fixture_observation_rejects_blank_fields(field, value):
    with pytest.raises(ValidationError, match="empty_observation_field") as exc:
        _fixture_observation(**{field: value})

    assert exc.value.errors()[0]["loc"] == (field,)


def test_application_contract_imports_without_runtime_frameworks_or_credentials():
    script = """
import socket
import sys

def reject_network(*args, **kwargs):
    raise AssertionError("application imports must be offline")

socket.getaddrinfo = reject_network
socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network

from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_runner import AgentRunner, AgentRunRequest

forbidden = {"app.agent_runtime", "langchain", "langchain_core", "langchain_openai",
             "langgraph", "langsmith", "openai", "fastapi", "sqlalchemy"}
for name in sys.modules:
    assert not any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden), name
"""
    subprocess.run([sys.executable, "-I", "-B", "-c", script], env=TRACING_OFF, check=True, timeout=10)


def test_langgraph_runner_executes_graph(monkeypatch, fixture_execution):
    research = _model(_tool_reply(), AIMessage(content="Синтетический fixture прочитан."))
    finalizer = _model(_analysis_reply())
    graph = create_agent_graph(research, finalizer)
    execution = AsyncMock(wraps=graph.ainvoke)
    monkeypatch.setattr(graph, "ainvoke", execution)
    runner: AgentRunner = LangGraphAgentRunner(graph)
    request = _request()
    research.ainvoke.assert_not_awaited()
    finalizer.ainvoke.assert_not_awaited()
    execution.assert_not_awaited()

    result = asyncio.run(runner.run(request))

    assert type(result) is AgentFinalResult
    assert result == _result(observations=(_fixture_observation(),))
    assert set(result.model_dump()) == {
        "summary", "status", "relevant_components", "assumptions", "observations", "blocked_reason",
    }
    assert research.ainvoke.await_count == 2
    fixture_execution.assert_awaited_once_with(component="task_service")
    finalizer.ainvoke.assert_awaited_once()
    execution.assert_awaited_once()
    assert execution.await_args.args[0] == {"goal": GOAL, "acceptance_criteria": list(CRITERIA)}
    assert request == _request()
    for model in (research, finalizer):
        for call in model.ainvoke.await_args_list:
            transcript = json.dumps([message.content for message in call.args[0]], ensure_ascii=False)
            assert str(request.task_id) not in transcript
            assert str(request.run_id) not in transcript
    assert isinstance(research.ainvoke.await_args.args[0][-1], ToolMessage)
    final_context = json.loads(finalizer.ainvoke.await_args.args[0][1].content)
    assert final_context["goal"] == GOAL
    assert final_context["acceptance_criteria"] == list(CRITERIA)
    assert final_context["observations"] == [result.observations[0].model_dump(mode="json")]


def test_langgraph_runner_runs_independent_tasks(fixture_execution):
    first = _request()
    second = _request("Уточнить входные данные", run_id=UUID("00000000-0000-4000-8000-000000000003"))
    research = _model(_tool_reply(), AIMessage(content="Первый анализ"), AIMessage(content="Второй анализ"))
    finalizer = _model(_analysis_reply("Первый результат"), _analysis_reply("Второй результат", "Нужны данные"))
    runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))

    async def run_both():
        return await runner.run(first), await runner.run(second)

    first_result, second_result = asyncio.run(run_both())

    assert first_result == _result(summary="Первый результат", observations=(_fixture_observation(),))
    assert second_result == _result(summary="Второй результат", status="blocked", blocked_reason="Нужны данные")
    assert research.ainvoke.await_count == 3
    fixture_execution.assert_awaited_once_with(component="task_service")
    assert finalizer.ainvoke.await_count == 2
    context = json.loads(finalizer.ainvoke.await_args.args[0][1].content)
    assert context == {
        "goal": second.goal,
        "acceptance_criteria": list(second.acceptance_criteria),
        "analysis_notes": ["Второй анализ"],
        "observations": [],
    }


@pytest.mark.parametrize("provider_failure", [True, False], ids=["provider", "graph-validation"])
def test_langgraph_runner_translates_error(monkeypatch, fixture_execution, provider_failure):
    provider_error = TimeoutError("Synthetic sensitive provider detail")
    research = _model(provider_error if provider_failure else AIMessage(content="  "))
    finalizer = _model()
    graph = create_agent_graph(research, finalizer)
    execution = AsyncMock(wraps=graph.ainvoke)
    monkeypatch.setattr(graph, "ainvoke", execution)
    runner = LangGraphAgentRunner(graph)

    with pytest.raises(AgentRunnerError) as exc:
        asyncio.run(runner.run(_request()))

    error = exc.value
    expected = "runner_timeout" if provider_failure else "graph_execution_failed"
    assert error.reason == str(error) == expected
    if provider_failure:
        assert error.__cause__ is provider_error
    else:
        assert isinstance(error.__cause__, AgentGraphError)
        assert error.__cause__.args == ("empty_model_response",)
    execution.assert_awaited_once()
    research.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()
    finalizer.ainvoke.assert_not_awaited()


def test_langgraph_runner_propagates_cancellation(fixture_execution):
    research = _model()
    finalizer = _model()

    async def cancel_during_model_call():
        started = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_release(_messages):
            started.set()
            await release.wait()
            return AIMessage(content="Synthetic reply")

        research.ainvoke.side_effect = wait_for_release
        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))
        task = asyncio.create_task(runner.run(_request()))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            assert task.cancelled()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=3)

    asyncio.run(cancel_during_model_call())

    research.ainvoke.assert_awaited_once()
    fixture_execution.assert_not_awaited()
    finalizer.ainvoke.assert_not_awaited()
