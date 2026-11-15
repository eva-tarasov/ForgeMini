import asyncio
import json
from functools import wraps
from types import MappingProxyType
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from model_test_support import offline_model_tests

from app.agent_runtime.agent_graph import MAX_MODEL_CALLS, AgentGraphError, call_model, create_agent_graph
from app.agent_runtime.finalization import FinalizationError
from app.agent_runtime.fixture_tools import create_tool_calling_model, read_fixture
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.structured_output import (
    StructuredOutputError,
    TaskAnalysis,
    create_structured_task_model,
    invoke_structured_analysis,
)
from app.agent_runtime.usage_capture import extract_token_usage, invoke_observed_model
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_result import FixtureObservation
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_service import AgentService
from app.services.agent_usage import (
    ModelCallOutcome as Outcome,
    ModelCallStage as Stage,
    ModelCallUsage,
    TokenUsage,
    UsageStatus as Status,
    capture_agent_usage,
    current_usage_collector,
)


COUNTS = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
TOKENS = TokenUsage(7, 3, 10)
ANALYSIS = TaskAnalysis(
    summary="Synthetic analysis", relevant_components=["task_service"], assumptions=["Fixture only"],
    blocked_reason=None,
)


def _message_with_usage(metadata):
    # Inject values as observed at the AIMessage boundary, without Pydantic coercion.
    return AIMessage(content="Synthetic analysis").model_copy(update={"usage_metadata": metadata})


def _model(*replies):
    runnable = create_autospec(Runnable, instance=True)
    runnable.ainvoke.side_effect = list(replies)
    return runnable


def _request(goal="Synthetic task"):
    return AgentRunRequest(uuid4(), uuid4(), goal, ("Use the training fixture",))


@pytest.mark.parametrize("envelope", [False, True])
def test_extracts_normalized_tokens_and_optional_details(envelope):
    raw = AIMessage(content="Synthetic", usage_metadata={
        **COUNTS,
        "input_token_details": {"cache_read": 6, "audio": 8},
        "output_token_details": {"reasoning": 2, "audio": 4},
    })
    response = MappingProxyType({"raw": raw, "parsing_error": ValueError("synthetic parse error")}) if envelope else raw
    assert extract_token_usage(response) == (Status.REPORTED, TokenUsage(7, 3, 10, 6, 2))


@pytest.mark.parametrize("response", [
    None, object(), {}, {"raw": object()}, AIMessage(content="Synthetic"),
    AIMessage(content="Synthetic", response_metadata={"token_usage": COUNTS}),
    {"raw": AIMessage(content="Synthetic")},
])
def test_missing_usage_has_no_fallback(response):
    assert extract_token_usage(response) == (Status.MISSING, None)


@pytest.mark.parametrize("metadata", [
    [], 1, {}, {"input_tokens": 7, "output_tokens": 3},
    {**COUNTS, "input_tokens": True}, {**COUNTS, "output_tokens": -1},
    {**COUNTS, "output_tokens": 3.0}, {**COUNTS, "total_tokens": "10"},
    {**COUNTS, "total_tokens": 11}, {**COUNTS, "total_tokens": False},
])
def test_invalid_core_usage_has_no_token_record(metadata):
    assert extract_token_usage(_message_with_usage(metadata)) == (Status.INVALID, None)


@pytest.mark.parametrize("input_details,output_details,expected", [
    (None, None, TOKENS), ([], "unknown", TOKENS),
    ({"cache_read": 8}, {"reasoning": 3}, TokenUsage(7, 3, 10, None, 3)),
    ({"cache_read": 0}, {"reasoning": True}, TokenUsage(7, 3, 10, 0, None)),
    ({"cache_read": 7.0}, {"reasoning": "3"}, TOKENS),
    ({"cache_read": -1}, {"reasoning": 4}, TOKENS),
    ({"cache_creation": 7}, {"audio": 3}, TOKENS),
])
def test_invalid_or_unknown_optional_details_do_not_drop_core_usage(input_details, output_details, expected):
    raw = _message_with_usage({
        **COUNTS, "input_token_details": input_details, "output_token_details": output_details, "unrelated": {},
    })
    assert extract_token_usage(raw) == (Status.REPORTED, expected)


def test_zero_usage_is_reported():
    raw = AIMessage(content="Synthetic", usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    assert extract_token_usage(raw) == (Status.REPORTED, TokenUsage(0, 0, 0))


@pytest.mark.parametrize("captured", [False, True], ids=["no-scope", "active-scope"])
def test_observation_preserves_response_identity_and_call_arguments(captured):
    response = _message_with_usage({**COUNTS, "input_tokens": True})
    model = _model(response)
    messages = [HumanMessage(content="Synthetic")]

    async def check():
        return await invoke_observed_model(model, messages, stage=Stage.RESEARCH)

    if captured:
        with capture_agent_usage(uuid4()) as collector:
            assert asyncio.run(check()) is response
        assert collector.snapshot().calls == (ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.INVALID, None),)
    else:
        assert current_usage_collector() is None
        assert asyncio.run(check()) is response
    model.ainvoke.assert_awaited_once_with(messages)
    assert model.ainvoke.await_args.args[0] is messages


@pytest.mark.parametrize("error", [RuntimeError("synthetic failure"), BaseException("synthetic interruption")])
def test_observation_preserves_exception_identity_and_never_retries(error):
    model = _model(error)
    with capture_agent_usage(uuid4()) as collector:
        with pytest.raises(type(error)) as exc:
            asyncio.run(invoke_observed_model(model, [], stage=Stage.FINALIZATION))
    assert exc.value is error
    assert collector.snapshot().calls == (ModelCallUsage(1, Stage.FINALIZATION, Outcome.ERROR, Status.MISSING, None),)
    model.ainvoke.assert_awaited_once_with([])


def test_closed_scope_does_not_record_late_child_return_or_start():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        raw = _message_with_usage(COUNTS)
        model = _model()

        async def wait(_messages):
            started.set()
            await release.wait()
            return raw

        model.ainvoke.side_effect = wait
        new_model = _model(raw)

        async def child():
            first = await invoke_observed_model(model, [], stage=Stage.RESEARCH)
            assert current_usage_collector() is None
            second = await invoke_observed_model(new_model, [], stage=Stage.FINALIZATION)
            return first, second

        try:
            with capture_agent_usage(uuid4()) as collector:
                task = asyncio.create_task(child())
                await asyncio.wait_for(started.wait(), timeout=3)
            snapshot = collector.snapshot()
            release.set()
            first, second = await asyncio.wait_for(task, timeout=3)
            assert first is second is raw
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        assert collector.snapshot() == snapshot
        assert snapshot.calls == (ModelCallUsage(1, Stage.RESEARCH, Outcome.STARTED, Status.MISSING, None),)
        model.ainvoke.assert_awaited_once()
        new_model.ainvoke.assert_awaited_once()

    asyncio.run(check())


def test_budget_rejection_and_standalone_structured_demo_do_not_start_usage_calls():
    research = _model()
    raw = _message_with_usage(COUNTS)
    structured = _model({"raw": raw, "parsed": ANALYSIS, "parsing_error": None})
    messages = (HumanMessage(content="Synthetic"),)

    async def check():
        with capture_agent_usage(uuid4()) as collector:
            with pytest.raises(AgentGraphError, match="^model_budget_exceeded$"):
                await call_model({"model_calls": MAX_MODEL_CALLS, "messages": []}, research)
            response = await invoke_structured_analysis(structured, messages=messages)
        assert response.raw is raw and response.analysis is ANALYSIS
        assert collector.snapshot().calls == ()

    asyncio.run(check())
    research.ainvoke.assert_not_awaited()
    structured.ainvoke.assert_awaited_once_with(list(messages))


def test_agent_usage_captures_all_model_calls(monkeypatch):
    request = _request()
    calls = []
    fixture = AsyncMock(wraps=read_fixture.coroutine)

    @wraps(read_fixture.coroutine)
    async def observe_fixture(**kwargs):
        return await fixture(**kwargs)

    monkeypatch.setattr(read_fixture, "coroutine", observe_fixture)
    replies = iter((
        ("tool_calls", {"content": None, "tool_calls": [{
            "id": "fixture-1", "type": "function",
            "function": {"name": "read_fixture", "arguments": '{"component":"task_service"}'},
        }]}, (12, 8, 4, 2)),
        ("stop", {"content": "The synthetic fixture was read."}, (24, 10, 7, 3)),
        ("stop", {"content": ANALYSIS.model_dump_json()}, (35, 11, 10, 9)),
    ))

    def respond(http_request):
        assert http_request.method == "POST"
        assert http_request.url == httpx.URL("https://forge-test.invalid/v1/chat/completions")
        calls.append(json.loads(http_request.content))
        finish_reason, message, (inputs, outputs, cached, reasoning) = next(replies)
        return httpx.Response(200, json={
            "id": f"chatcmpl-usage-{len(calls)}", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", **message}}],
            "usage": {
                "prompt_tokens": inputs, "completion_tokens": outputs, "total_tokens": inputs + outputs,
                "prompt_tokens_details": {"cached_tokens": cached},
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        })

    async def check():
        transport = httpx.MockTransport(respond)
        with httpx.Client(transport=transport, trust_env=False) as sync_client:
            async with httpx.AsyncClient(transport=transport, trust_env=False) as async_client:
                model = ChatOpenAI(
                    model="gpt-4o-mini", api_key="synthetic-openai-key", base_url="https://forge-test.invalid/v1",
                    openai_proxy="", use_responses_api=False, max_retries=0,
                    http_client=sync_client, http_async_client=async_client,
                )
                graph = create_agent_graph(create_tool_calling_model(model), create_structured_task_model(model))
                runner = LangGraphAgentRunner(graph)
                invocation = AsyncMock(wraps=runner.run)
                with capture_agent_usage(request.run_id) as collector:
                    async with asyncio.timeout(5):
                        result = await invocation(request)
                invocation.assert_awaited_once_with(request)
        assert collector.closed and current_usage_collector() is None
        return result, collector.snapshot()

    result, snapshot = asyncio.run(check())
    assert len(calls) == 3
    fixture.assert_awaited_once_with(component="task_service")
    assert calls[0]["tools"][0]["function"]["name"] == "read_fixture"
    assert calls[1]["messages"][-1]["role"] == "tool"
    assert calls[2]["response_format"]["type"] == "json_schema"
    assert (result.status, result.summary) == ("ready_for_verification", ANALYSIS.summary)
    assert result.relevant_components == ("task_service",) and result.assumptions == ("Fixture only",)
    assert result.blocked_reason is None
    assert result.observations == (FixtureObservation(
        component="task_service", tool_call_id="fixture-1",
        content="Training fixture (synthetic): an idempotency key is scoped to its workspace. "
                "The same key may be used in a different workspace.",
    ),)
    assert snapshot.run_id == request.run_id
    assert snapshot.calls == (
        ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TokenUsage(12, 8, 20, 4, 2)),
        ModelCallUsage(2, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TokenUsage(24, 10, 34, 7, 3)),
        ModelCallUsage(3, Stage.FINALIZATION, Outcome.RETURNED, Status.REPORTED, TokenUsage(35, 11, 46, 10, 9)),
    )
    assert snapshot.model_calls_started == snapshot.reported_calls == 3 and snapshot.usage_complete
    assert snapshot.unknown_usage_calls == 0
    assert (snapshot.known_input_tokens, snapshot.known_output_tokens, snapshot.known_total_tokens) == (71, 29, 100)


def _tool_reply():
    return AIMessage(content="", tool_calls=[{
        "id": "fixture-1", "name": "read_fixture", "args": {"component": "task_service"},
    }], usage_metadata=COUNTS)


def test_usage_with_invalid_metadata_preserves_successful_graph_result():
    async def check():
        request = _request()
        research = _message_with_usage({**COUNTS, "input_tokens": True})
        envelope = {"raw": AIMessage(content="Synthetic analysis"), "parsed": ANALYSIS, "parsing_error": None}
        runner = LangGraphAgentRunner(create_agent_graph(_model(research, research), _model(envelope, envelope)))
        async with asyncio.timeout(5):
            original = await runner.run(request)
            with capture_agent_usage(request.run_id) as collector:
                observed = await runner.run(request)
        assert observed == original
        snapshot = collector.snapshot()
        assert snapshot.calls == (
            ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.INVALID, None),
            ModelCallUsage(2, Stage.FINALIZATION, Outcome.RETURNED, Status.MISSING, None),
        )
        assert snapshot.unknown_usage_calls == 2 and not snapshot.usage_complete

    asyncio.run(check())


def _assert_failed_graph_usage(case):
    async def check():
        initial = _message_with_usage(COUNTS)
        raw = _message_with_usage({"input_tokens": 13, "output_tokens": 5, "total_tokens": 18})
        provider_error = RuntimeError("synthetic model error")
        research = _model(initial)
        finalizer = _model({"raw": raw, "parsed": ANALYSIS, "parsing_error": None})
        expected = [ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TOKENS)]
        if case == "invalid-research":
            research.ainvoke.side_effect = [_message_with_usage(COUNTS).model_copy(update={"content": " "})]
            reason, cause_type = "graph_execution_failed", AgentGraphError
        elif case == "parsing-failure":
            finalizer.ainvoke.side_effect = [{"raw": raw, "parsed": None, "parsing_error": ValueError("synthetic")}]
            reason, cause_type = "structured_output_invalid", StructuredOutputError
            expected.append(ModelCallUsage(2, Stage.FINALIZATION, Outcome.RETURNED, Status.REPORTED, TokenUsage(13, 5, 18)))
        elif case == "invalid-finalization":
            finalizer.ainvoke.side_effect = [{
                "raw": raw, "parsed": ANALYSIS.model_copy(update={"summary": " "}), "parsing_error": None,
            }]
            reason, cause_type = "structured_output_invalid", FinalizationError
            expected.append(ModelCallUsage(2, Stage.FINALIZATION, Outcome.RETURNED, Status.REPORTED, TokenUsage(13, 5, 18)))
        else:
            research.ainvoke.side_effect = [_tool_reply(), provider_error]
            reason, cause_type = "execution_failed", RuntimeError
            expected.append(ModelCallUsage(2, Stage.RESEARCH, Outcome.ERROR, Status.MISSING, None))

        request = _request()
        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))
        with capture_agent_usage(request.run_id) as collector:
            async with asyncio.timeout(5):
                with pytest.raises(AgentRunnerError, match=f"^{reason}$") as exc:
                    await runner.run(request)
        assert collector.closed and current_usage_collector() is None
        assert type(exc.value.__cause__) is cause_type
        if case == "model-error":
            assert exc.value.__cause__ is provider_error
        if case == "parsing-failure":
            assert exc.value.__cause__.raw is raw
        assert research.ainvoke.await_count == sum(call.stage is Stage.RESEARCH for call in expected)
        assert finalizer.ainvoke.await_count == sum(call.stage is Stage.FINALIZATION for call in expected)
        assert collector.snapshot().calls == tuple(expected)

    asyncio.run(check())


def _assert_interrupted_graph_usage(monkeypatch, phase):
    async def check():
        started, cleaned = asyncio.Event(), asyncio.Event()
        received = []
        collectors = []
        request = _request()
        research = _model()
        finalizer = _model()

        async def wait_for_response():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                received.append(error)
                raise
            finally:
                cleaned.set()

        async def research_reply(messages):
            if not isinstance(messages[-1], ToolMessage):
                return _tool_reply()
            if phase == "research":
                return await wait_for_response()
            return _message_with_usage({"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

        async def finalize(_messages):
            await wait_for_response()

        research.ainvoke.side_effect = research_reply
        finalizer.ainvoke.side_effect = finalize
        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))
        invocation = AsyncMock(wraps=runner.run)
        monkeypatch.setattr(runner, "run", invocation)
        service = AgentService(None, runner, execution_timeout_seconds=0.5)
        fail, complete = AsyncMock(), AsyncMock()
        monkeypatch.setattr(service, "_claim", AsyncMock(return_value=request))
        monkeypatch.setattr(service, "_fail", fail)
        monkeypatch.setattr(service, "_complete", complete)

        async def execute():
            try:
                with capture_agent_usage(request.run_id) as collector:
                    collectors.append(collector)
                    if phase == "deadline":
                        return await service.execute_run(request.run_id)
                    return await runner.run(request)
            finally:
                assert current_usage_collector() is None

        task = asyncio.create_task(execute())
        try:
            async with asyncio.timeout(5):
                await started.wait()
                if phase == "deadline":
                    with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$") as exc:
                        await task
                    assert isinstance(exc.value.__cause__, TimeoutError)
                    fail.assert_awaited_once_with(
                        request.run_id, AgentFailure(AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED)
                    )
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert task.cancelled()
                    fail.assert_not_called()
                await cleaned.wait()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)

        assert len(received) == 1
        complete.assert_not_called()
        invocation.assert_awaited_once_with(request)
        assert research.ainvoke.await_count == 2
        assert finalizer.ainvoke.await_count == (0 if phase == "research" else 1)
        (collector,) = collectors
        assert collector.closed
        snapshot = collector.snapshot()
        expected = [ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TOKENS)]
        if phase == "research":
            expected.append(ModelCallUsage(2, Stage.RESEARCH, Outcome.CANCELLED, Status.MISSING, None))
            known = (7, 3, 10)
        else:
            expected.extend((
                ModelCallUsage(2, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TokenUsage(11, 4, 15)),
                ModelCallUsage(3, Stage.FINALIZATION, Outcome.CANCELLED, Status.MISSING, None),
            ))
            known = (18, 7, 25)
        assert snapshot.calls == tuple(expected)
        assert snapshot.unknown_usage_calls == 1 and not snapshot.usage_complete
        assert (snapshot.known_input_tokens, snapshot.known_output_tokens, snapshot.known_total_tokens) == known

    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "invalid-research", "parsing-failure", "invalid-finalization", "model-error",
    "cancel-research", "cancel-finalization", "deadline",
])
def test_agent_usage_preserves_failed_and_cancelled_calls(monkeypatch, case):
    if case in ("cancel-research", "cancel-finalization", "deadline"):
        _assert_interrupted_graph_usage(monkeypatch, case.removeprefix("cancel-"))
    else:
        _assert_failed_graph_usage(case)


def test_agent_usage_isolated_concurrent_graphs():
    async def check():
        reached = asyncio.Event()
        stages = []
        collectors = {}
        numbers = {"alpha": (3, 5), "beta": (11, 13)}
        research, finalizer = _model(), _model()

        async def research_reply(messages):
            goal = json.loads(messages[-1].content)["goal"]
            assert current_usage_collector() is collectors[goal]
            stages.append((goal, Stage.RESEARCH))
            if len(stages) == 2:
                reached.set()
            await reached.wait()
            inputs = numbers[goal][0]
            return _message_with_usage({"input_tokens": inputs, "output_tokens": 1, "total_tokens": inputs + 1})

        async def final_reply(messages):
            goal = json.loads(messages[-1].content)["goal"]
            assert current_usage_collector() is collectors[goal]
            stages.append((goal, Stage.FINALIZATION))
            inputs = numbers[goal][1]
            raw = _message_with_usage({"input_tokens": inputs, "output_tokens": 2, "total_tokens": inputs + 2})
            return {"raw": raw, "parsed": ANALYSIS, "parsing_error": None}

        research.ainvoke.side_effect = research_reply
        finalizer.ainvoke.side_effect = final_reply
        runner = LangGraphAgentRunner(create_agent_graph(research, finalizer))

        async def execute(request):
            with capture_agent_usage(request.run_id) as collector:
                collectors[request.goal] = collector
                result = await runner.run(request)
            assert collector.closed and current_usage_collector() is None
            return result, collector.snapshot()

        requests = [_request(goal) for goal in numbers]
        async with asyncio.timeout(5):
            results = await asyncio.gather(*(execute(request) for request in requests))

        assert collectors["alpha"] is not collectors["beta"]
        assert {stage for stage in stages[:2]} == {("alpha", Stage.RESEARCH), ("beta", Stage.RESEARCH)}
        assert research.ainvoke.await_count == finalizer.ainvoke.await_count == 2
        for request, (result, snapshot) in zip(requests, results, strict=True):
            assert result.summary == ANALYSIS.summary
            assert result.observations == ()
            assert snapshot.run_id == request.run_id and snapshot.usage_complete
            first, second = numbers[request.goal]
            assert snapshot.calls == (
                ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TokenUsage(first, 1, first + 1)),
                ModelCallUsage(2, Stage.FINALIZATION, Outcome.RETURNED, Status.REPORTED, TokenUsage(second, 2, second + 2)),
            )
            assert (snapshot.known_input_tokens, snapshot.known_output_tokens, snapshot.known_total_tokens) == (
                first + second, 3, first + second + 3,
            )
        assert current_usage_collector() is None

    asyncio.run(check())
