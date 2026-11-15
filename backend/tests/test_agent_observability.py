import asyncio
import io
import json
import logging
import math
import re
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.core.logging import AgentJsonFormatter, AgentStreamHandler, JsonFormatter, configure_agent_logger
from app.domain.execution import AgentRunStatus
from app.domain.transitions import InvalidAgentRunTransition
from app.services import agent_observability, agent_service
from app.services.active_runs import ActiveRunRegistry
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_observability import AgentLogContext, AgentObserver, AgentOperation, AgentPhase
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunNotFoundError, StaleAgentRunStatusError
from app.services.agent_service import AgentService, AgentUsagePersistenceError
from app.services.agent_usage import ModelCallOutcome, ModelCallStage, TokenUsage, UsageStatus, current_usage_collector


_COMMON = {"timestamp", "level", "event", "operation_id", "operation", "run_id", "task_id"}
_FINISH = {"outcome", "duration_ms", "error_kind"}
_USAGE = {
    "model_calls_started", "reported_calls", "unknown_usage_calls", "known_input_tokens",
    "known_output_tokens", "known_total_tokens", "usage_complete", "priced_calls",
    "unknown_cost_calls", "known_cost_usd", "cost_complete",
}
_EXTRA = {
    "agent_operation_started": set(),
    "agent_operation_finished": _FINISH,
    "agent_phase_started": {"phase"},
    "agent_phase_finished": _FINISH | {"phase"},
    "agent_failure_classified": {"failure_code"},
    "agent_usage_observed": _USAGE,
}


@pytest.fixture
def agent_log_capture():
    logger = logging.getLogger("forge.agent")
    original_handlers = logger.handlers[:]
    original_level, original_propagate = logger.level, logger.propagate
    handler = _owned_handler(configure_agent_logger())
    stream = io.StringIO()
    original_stream = handler.setStream(stream)
    try:
        yield stream
    finally:
        handler.setStream(original_stream)
        if handler not in original_handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        stream.close()


def _owned_handler(logger):
    handler, = [item for item in logger.handlers if item.name == "forge.agent.json"]
    return handler


def read_agent_logs(stream):
    groups = defaultdict(list)
    for line in stream.getvalue().splitlines():
        row = json.loads(line)
        assert row.keys() == _COMMON | _EXTRA[row["event"]]
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", row["timestamp"])
        assert datetime.fromisoformat(row["timestamp"]).utcoffset() == timedelta(0)
        for field in ("run_id", "task_id", "operation_id"):
            if field != "task_id" or row[field] is not None:
                assert str(UUID(row[field])) == row[field]
        if "duration_ms" in row:
            assert math.isfinite(row["duration_ms"]) and row["duration_ms"] >= 0
            if row["outcome"] != "error":
                assert row["error_kind"] is None
        warning = row["event"] == "agent_failure_classified" or row.get("outcome") == "error"
        assert row["level"] == ("WARNING" if warning else "INFO")
        groups[row["operation_id"]].append(row)
    return groups


def assert_agent_operation(rows, operation, run_id, phases, *, task_id=None, outcome="completed", error=None):
    assert rows[0]["event"] == "agent_operation_started"
    assert rows[0]["task_id"] is None
    assert rows[-1]["event"] == "agent_operation_finished"
    assert [row["event"] for row in rows if row["event"].startswith("agent_operation_")] == [
        "agent_operation_started", "agent_operation_finished",
    ]
    assert (rows[-1]["outcome"], rows[-1]["error_kind"]) == (outcome, error)
    assert {row["operation"] for row in rows} == {operation}
    assert {row["run_id"] for row in rows} == {str(run_id)}

    if operation == "execute" and phases[0] == ("claim", "completed"):
        assert task_id is not None
        assert [row["task_id"] for row in rows] == [None, None] + [str(task_id)] * (len(rows) - 2)
    else:
        assert all(row["task_id"] is None for row in rows)

    phase_steps = [
        (row["event"], row["phase"], row.get("outcome")) for row in rows if "phase" in row
    ]
    assert phase_steps == [
        step for phase, phase_outcome in phases for step in (
            ("agent_phase_started", phase, None), ("agent_phase_finished", phase, phase_outcome),
        )
    ]


def _execute_phases(*later):
    return [("claim", "completed"), *later]


_READY_PHASES = _execute_phases(
    ("runner", "completed"), ("result_validation", "completed"), ("result_persistence", "completed"),
)
_CANCEL_PHASES = [("cancellation_persistence", "completed"), ("cancellation_signal", "completed")]


_CANARY = "SYNTHETIC_PRIVATE_"
_READY = AgentFinalResult(
    summary=_CANARY + "SUMMARY", status="ready_for_verification", relevant_components=(), assumptions=(),
    observations=(FixtureObservation(
        component="task_service", tool_call_id=_CANARY + "TOOL_CALL", content=_CANARY + "OBSERVATION",
    ),),
    blocked_reason=None,
)
_BLOCKED = _READY.model_copy(update={"status": "blocked", "blocked_reason": _CANARY + "BLOCKER"})


def _request():
    return AgentRunRequest(uuid4(), uuid4(), _CANARY + "GOAL", (_CANARY + "CRITERION",))


def _start_usage():
    collector = current_usage_collector()
    call = collector.start_call(ModelCallStage.RESEARCH)
    collector.finish_call(
        call, outcome=ModelCallOutcome.RETURNED, usage_status=UsageStatus.REPORTED,
        tokens=TokenUsage(12, 8, 20, 4, 2),
    )
    return collector


def _service(monkeypatch, requests, run, **configuration):
    mapping = {request.run_id: request for request in requests}
    runner = AsyncMock(side_effect=run)
    registry = ActiveRunRegistry()
    service = AgentService(Mock(side_effect=AssertionError("Unexpected session")), SimpleNamespace(run=runner),
                           active_runs=registry, **configuration)
    monkeypatch.setattr(service, "_claim", AsyncMock(side_effect=mapping.__getitem__))
    monkeypatch.setattr(service, "_complete", AsyncMock())
    monkeypatch.setattr(service, "_fail", AsyncMock())
    monkeypatch.setattr(service, "_persist_usage", AsyncMock())
    return service, runner, registry


def test_agent_observability_formats_separate_json_and_uses_monotonic_time(agent_log_capture, monkeypatch):
    agent = configure_agent_logger()
    http = logging.getLogger("forge.http")
    root = logging.getLogger()
    http_and_root = [(item.level, item.propagate, item.handlers[:]) for item in (http, root)]
    before = [*agent.handlers, logging.NullHandler()]
    monkeypatch.setattr(agent, "handlers", before[:])
    owned = _owned_handler(agent)
    assert configure_agent_logger() is agent
    assert agent.handlers == before
    assert isinstance(owned, AgentStreamHandler) and owned.name == "forge.agent.json"
    assert isinstance(owned.formatter, AgentJsonFormatter)
    assert (agent.name, agent.level, agent.propagate) == ("forge.agent", logging.INFO, False)

    ticks = iter([1_000_000, 2_000_000, 4_500_000, 8_000_000])
    monkeypatch.setattr(agent_observability, "perf_counter_ns", lambda: next(ticks))
    context = AgentLogContext(uuid4(), AgentOperation.EXECUTE)
    observer = AgentObserver(context)
    with observer.operation(), observer.phase(AgentPhase.CLAIM):
        context.task_id = uuid4()
    rows = read_agent_logs(agent_log_capture)[str(context.operation_id)]
    assert_agent_operation(rows, "execute", context.run_id, [("claim", "completed")], task_id=context.task_id)
    assert [row["duration_ms"] for row in rows if "duration_ms" in row] == [2.5, 7.0]

    http_record = logging.LogRecord("forge.http", logging.INFO, "", 0, "http_request", (), None)
    http_record.http_fields = {"request_id": "ID", "method": "GET", "route": "/health", "status_code": 200,
                               "duration_ms": 1}
    assert json.loads(JsonFormatter().format(http_record)).keys() == {
        "timestamp", "level", "event", "request_id", "method", "route", "status_code", "duration_ms",
    }
    assert [(item.level, item.propagate, item.handlers[:]) for item in (http, root)] == http_and_root


def test_agent_formatter_disregards_record_text_and_rejects_unapproved_fields(agent_log_capture, capsys):
    logger = configure_agent_logger()
    context = AgentLogContext(uuid4(), AgentOperation.CANCEL)
    captured = []

    def capture(record):
        captured.append(record)
        return True

    logger.addFilter(capture)
    try:
        with AgentObserver(context).operation():
            pass
    finally:
        logger.removeFilter(capture)
    record = captured[-1]
    record.created = 0.123
    record.getMessage = Mock(side_effect=AssertionError(_CANARY + "GET_MESSAGE"))
    record.msg = _CANARY + "LOG_MESSAGE"
    record.exc_info = (ValueError, ValueError(_CANARY + "EXCEPTION"), None)
    record.exc_text = record.stack_info = record.pathname = _CANARY + "STACK"
    record.private = _CANARY + "EXTRA_RECORD_FIELD"
    formatter = AgentJsonFormatter()

    class DerivedUUID(UUID):
        def __str__(self):
            return _CANARY + "UUID"

    record.agent_fields["run_id"] = DerivedUUID(int=context.run_id.int)
    line = formatter.format(record)
    assert json.loads(line)["timestamp"] == "1970-01-01T00:00:00.123Z"
    assert json.loads(line)["run_id"] == str(context.run_id)
    assert _CANARY not in line
    good = record.agent_fields
    for invalid in (
        {"debug": _CANARY}, {"event": _CANARY}, {"operation_id": str(context.operation_id)},
        {"operation": "cancel"}, {"task_id": _CANARY}, {"outcome": "completed"},
        {"duration_ms": float("nan")}, {"duration_ms": -1}, {"error_kind": _CANARY},
    ):
        record.agent_fields = {**good, **invalid}
        with pytest.raises(ValueError):
            formatter.format(record)
    _owned_handler(logger).handle(record)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_agent_observer_preserves_non_cancellation_base_exceptions(exception_type, agent_log_capture):
    context = AgentLogContext(uuid4(), AgentOperation.CANCEL)
    error = exception_type(_CANARY + "BASE_EXCEPTION")
    with pytest.raises(exception_type) as caught:
        observer = AgentObserver(context)
        with observer.operation(), observer.phase(AgentPhase.CANCELLATION_PERSISTENCE):
            raise error
    assert caught.value is error
    rows, = read_agent_logs(agent_log_capture).values()
    assert_agent_operation(rows, "cancel", context.run_id, [("cancellation_persistence", "error")],
                           outcome="error", error="operation_error")
    assert _CANARY not in agent_log_capture.getvalue()


def test_agent_observability_concurrent_operations(agent_log_capture, monkeypatch):
    first, second = _request(), _request()

    async def check():
        entered = {request.run_id: asyncio.Event() for request in (first, second)}
        release = {request.run_id: asyncio.Event() for request in (first, second)}
        committed = asyncio.Event()

        async def run(request):
            entered[request.run_id].set()
            try:
                await release[request.run_id].wait()
            except asyncio.CancelledError:
                assert committed.is_set()
                raise
            return _READY

        @asynccontextmanager
        async def session():
            yield object()

        async def cancel(run_id):
            assert run_id == first.run_id
            committed.set()

        service, runner, registry = _service(monkeypatch, (first, second), run)
        monkeypatch.setattr(service, "_session_factory", session)
        monkeypatch.setattr(agent_service, "AgentRunService", lambda _: SimpleNamespace(cancel_execution=cancel))
        tasks = [asyncio.create_task(service.execute_run(item.run_id)) for item in (first, second)]
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*(event.wait() for event in entered.values()))
                await service.cancel_run(first.run_id)
                release[second.run_id].set()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                assert await tasks[1] is _READY
                assert await service.execute_run(second.run_id) is _READY
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert first.run_id not in registry and second.run_id not in registry
        assert [item.args[0] for item in runner.await_args_list] == [first, second, second]

    asyncio.run(check())
    groups = read_agent_logs(agent_log_capture)
    assert len(groups) == 4
    first_rows = [rows for rows in groups.values() if rows[0]["run_id"] == str(first.run_id)]
    execute, cancel = sorted(first_rows, key=lambda rows: rows[0]["operation"], reverse=True)
    assert_agent_operation(execute, "execute", first.run_id, _execute_phases(("runner", "interrupted")),
                           task_id=first.task_id, outcome="interrupted")
    assert_agent_operation(cancel, "cancel", first.run_id, _CANCEL_PHASES)
    for rows in groups.values():
        if rows[0]["run_id"] == str(second.run_id):
            assert_agent_operation(rows, "execute", second.run_id, _READY_PHASES, task_id=second.task_id)


class _BrokenStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.attempts = 0
        self.active_errors = []

    def write(self, _text):
        self.attempts += 1
        self.active_errors.append(sys.exception())
        raise OSError(_CANARY + "STREAM_WRITE")


class _RaisingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def emit(self, _record):
        self.attempts += 1
        raise RuntimeError(_CANARY + "HANDLER")


@pytest.mark.parametrize("case", ["ready", "blocked", "runner-plus-usage", "result-write", "usage-write", "external-cancel"])
def test_agent_observability_does_not_leak_or_mask_errors(case, agent_log_capture, monkeypatch, capsys):
    request = _request()
    cause = ValueError(_CANARY + "CAUSE")
    runner_error = AgentRunnerError(_CANARY + "REASON")
    result_error = RuntimeError(_CANARY + "RESULT_WRITE")
    result_error.__cause__ = cause
    usage_error = RuntimeError(_CANARY + "USAGE_WRITE")
    cancelled = []
    logger = configure_agent_logger()
    stream = _BrokenStream()
    broken = AgentStreamHandler(stream)
    broken.setFormatter(AgentJsonFormatter())
    raising = _RaisingHandler()
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, broken, raising])
    monkeypatch.setattr(logging, "raiseExceptions", True)

    async def check():
        entered = asyncio.Event()

        async def run(_request):
            if case != "blocked":
                _start_usage()
            if case == "runner-plus-usage":
                raise runner_error from cause
            if case == "external-cancel":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    cancelled.append(error)
                    raise
            return _BLOCKED if case == "blocked" else _READY

        service, runner, registry = _service(monkeypatch, (request,), run)
        if case == "result-write":
            service._complete.side_effect = result_error
        if case in {"usage-write", "runner-plus-usage", "external-cancel"}:
            service._persist_usage.side_effect = usage_error

        async with asyncio.timeout(3):
            if case == "external-cancel":
                task = asyncio.create_task(service.execute_run(request.run_id))
                try:
                    await entered.wait()
                    task.cancel(_CANARY + "CANCELLATION")
                    with pytest.raises(asyncio.CancelledError) as caught:
                        await task
                    assert caught.value is cancelled[0]
                    assert caught.value.__notes__ == ["agent_usage_persistence_failed"]
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            elif case == "runner-plus-usage":
                with pytest.raises(AgentRunnerError) as caught:
                    await service.execute_run(request.run_id)
                assert caught.value is runner_error and caught.value.__cause__ is cause
                assert caught.value.__notes__ == ["agent_usage_persistence_failed"]
            elif case == "result-write":
                with pytest.raises(RuntimeError) as caught:
                    await service.execute_run(request.run_id)
                assert caught.value is result_error and caught.value.__cause__ is cause
            elif case == "usage-write":
                with pytest.raises(AgentUsagePersistenceError) as caught:
                    await service.execute_run(request.run_id)
                assert caught.value.__cause__ is usage_error
                service._complete.assert_awaited_once_with(request.run_id, _READY)
            else:
                assert await service.execute_run(request.run_id) is (_BLOCKED if case == "blocked" else _READY)
        if case == "runner-plus-usage":
            service._fail.assert_awaited_once_with(request.run_id, AgentFailure(AgentFailureCode.EXECUTION_FAILED))
        else:
            service._fail.assert_not_called()
        runner.assert_awaited_once_with(request)
        assert request.run_id not in registry
        assert service._persist_usage.await_count == (0 if case == "blocked" else 1)

    asyncio.run(check())
    rows, = read_agent_logs(agent_log_capture).values()
    assert stream.attempts == raising.attempts == len(rows)
    primary = {
        "runner-plus-usage": runner_error, "result-write": result_error,
        "usage-write": usage_error, "external-cancel": cancelled[0] if cancelled else None,
    }.get(case)
    if primary is not None:
        assert any(active is primary for active in stream.active_errors)
    assert _CANARY not in agent_log_capture.getvalue()
    assert capsys.readouterr().err == ""
    usage = [row for row in rows if row["event"] == "agent_usage_observed"]
    if case == "blocked":
        assert usage == []
    else:
        usage_row, = usage
        assert {key: usage_row[key] for key in _USAGE} == {
            "model_calls_started": 1, "reported_calls": 1, "unknown_usage_calls": 0,
            "known_input_tokens": 12, "known_output_tokens": 8, "known_total_tokens": 20,
            "usage_complete": True, "priced_calls": 0, "unknown_cost_calls": 1,
            "known_cost_usd": "0", "cost_complete": False,
        }
        assert rows[rows.index(usage_row) + 1]["phase"] == "usage_persistence"
    phases = {
        "ready": _READY_PHASES + [("usage_persistence", "completed")],
        "blocked": _READY_PHASES,
        "runner-plus-usage": _execute_phases(
            ("runner", "error"), ("failure_persistence", "completed"), ("usage_persistence", "error"),
        ),
        "result-write": _READY_PHASES[:-1] + [("result_persistence", "error"), ("usage_persistence", "completed")],
        "usage-write": _READY_PHASES + [("usage_persistence", "error")],
        "external-cancel": _execute_phases(("runner", "interrupted"), ("usage_persistence", "error")),
    }[case]
    codes = [row["failure_code"] for row in rows if "failure_code" in row]
    assert codes == (["execution_failed"] if case == "runner-plus-usage" else [])
    outcome = "completed" if case in {"ready", "blocked"} else "interrupted" if case == "external-cancel" else "error"
    assert_agent_operation(rows, "execute", request.run_id, phases, task_id=request.task_id,
                           outcome=outcome, error="operation_error" if outcome == "error" else None)


@pytest.mark.parametrize("case", ["deadline", "invalid-result", "stale", "missing"])
def test_agent_observability_records_safe_execution_failures(case, agent_log_capture, monkeypatch):
    request = _request()
    stale = StaleAgentRunStatusError(AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)
    missing = AgentRunNotFoundError(_CANARY)

    async def run(_request):
        if case == "deadline":
            await asyncio.Event().wait()
        return object()

    service, runner, registry = _service(
        monkeypatch, (request,), run, execution_timeout_seconds=0.05 if case == "deadline" else 60,
    )
    claim_error = {"stale": stale, "missing": missing}.get(case)
    if claim_error is not None:
        service._claim.side_effect = claim_error

    async def check():
        async with asyncio.timeout(3):
            with pytest.raises(type(claim_error) if claim_error is not None else AgentRunnerError) as caught:
                await service.execute_run(request.run_id)
            return caught.value

    error = asyncio.run(check())
    assert request.run_id not in registry
    service._complete.assert_not_called()
    service._persist_usage.assert_not_called()
    rows, = read_agent_logs(agent_log_capture).values()
    if claim_error is not None:
        assert error is claim_error
        service._fail.assert_not_called()
        runner.assert_not_called()
        phases = [("claim", "error")]
        error_kind = "stale_run_status" if case == "stale" else "run_not_found"
    else:
        runner.assert_awaited_once_with(request)
        if case == "deadline":
            assert error.reason == "execution_deadline_exceeded" and isinstance(error.__cause__, TimeoutError)
            phases = _execute_phases(("runner", "error"))
        else:
            assert error.reason == "invalid_runner_result" and error.__cause__ is None
            phases = _execute_phases(("runner", "completed"), ("result_validation", "error"))
        error_kind = "operation_error"
        phases.append(("failure_persistence", "completed"))
        failure_row, = [row for row in rows if "failure_code" in row]
        assert failure_row["failure_code"] == error.reason
        assert rows[rows.index(failure_row) + 1]["phase"] == "failure_persistence"
        service._fail.assert_awaited_once_with(request.run_id, AgentFailure(AgentFailureCode(error.reason)))
    assert_agent_operation(rows, "execute", request.run_id, phases, task_id=request.task_id,
                           outcome="error", error=error_kind)


@pytest.mark.parametrize("failure_point", ["estimator", "configuration"])
def test_agent_observability_telemetry_preparation_failure_keeps_usage_write(
    failure_point, agent_log_capture, monkeypatch,
):
    request = _request()

    async def run(_request):
        _start_usage()
        return _READY

    failure = Mock(side_effect=RuntimeError(_CANARY + "TELEMETRY"))
    name = "estimate_agent_cost" if failure_point == "estimator" else "configure_agent_logger"
    monkeypatch.setattr(agent_observability, name, failure)
    service, runner, registry = _service(monkeypatch, (request,), run)
    assert asyncio.run(service.execute_run(request.run_id)) is _READY
    assert failure.called and request.run_id not in registry
    runner.assert_awaited_once_with(request)
    service._persist_usage.assert_awaited_once()
    assert service._persist_usage.await_args.args[0].known_total_tokens == 20
    if failure_point == "estimator":
        rows, = read_agent_logs(agent_log_capture).values()
        assert_agent_operation(rows, "execute", request.run_id,
                               _READY_PHASES + [("usage_persistence", "completed")], task_id=request.task_id)
        assert all(row["event"] != "agent_usage_observed" for row in rows)
    else:
        assert not agent_log_capture.getvalue()


@pytest.mark.parametrize("committed", [True, False], ids=["close-failure-after-commit", "rejected-transition"])
def test_agent_observability_cancellation_signal_follows_confirmed_commit(committed, agent_log_capture, monkeypatch):
    request = _request()
    close_error = RuntimeError(_CANARY + "SESSION_CLOSE")
    transition_error = InvalidAgentRunTransition(AgentRunStatus.FAILED, AgentRunStatus.CANCELLED)

    @asynccontextmanager
    async def session():
        yield object()
        raise close_error

    cancel = AsyncMock(side_effect=None if committed else transition_error)
    registry = ActiveRunRegistry()
    signal = Mock(wraps=registry.request_cancel)
    monkeypatch.setattr(registry, "request_cancel", signal)
    monkeypatch.setattr(agent_service, "AgentRunService", lambda _: SimpleNamespace(cancel_execution=cancel))
    service = AgentService(session, SimpleNamespace(run=AsyncMock()), active_runs=registry)
    expected = close_error if committed else transition_error
    with pytest.raises(type(expected)) as caught:
        asyncio.run(service.cancel_run(request.run_id))
    assert caught.value is expected
    cancel.assert_awaited_once_with(request.run_id)
    phases = [("cancellation_persistence", "error")]
    if committed:
        signal.assert_called_once_with(request.run_id)
        phases.append(("cancellation_signal", "completed"))
    else:
        signal.assert_not_called()
    rows, = read_agent_logs(agent_log_capture).values()
    kind = "operation_error" if committed else "invalid_transition"
    assert_agent_operation(rows, "cancel", request.run_id, phases, outcome="error", error=kind)
    assert rows[2]["error_kind"] == kind


def test_agent_observability_cost_summary_keeps_only_known_subtotal(agent_log_capture, monkeypatch):
    request = _request()
    tariff = ModelTariff(_CANARY + "PROVIDER", _CANARY + "MODEL", _CANARY + "TARIFF",
                         Decimal("2.00"), Decimal("8.00"), Decimal("0.50"))

    async def run(_request):
        collector = _start_usage()
        call = collector.start_call(ModelCallStage.FINALIZATION)
        collector.finish_call(call, outcome=ModelCallOutcome.RETURNED, usage_status=UsageStatus.MISSING, tokens=None)
        return _READY

    service, runner, _ = _service(monkeypatch, (request,), run, pricing=AgentPricing(tariff, tariff))
    assert asyncio.run(service.execute_run(request.run_id)) is _READY
    runner.assert_awaited_once_with(request)
    rows, = read_agent_logs(agent_log_capture).values()
    usage, = [row for row in rows if row["event"] == "agent_usage_observed"]
    assert {key: usage[key] for key in _USAGE} == {
        "model_calls_started": 2, "reported_calls": 1, "unknown_usage_calls": 1,
        "known_input_tokens": 12, "known_output_tokens": 8, "known_total_tokens": 20,
        "usage_complete": False, "priced_calls": 1, "unknown_cost_calls": 1,
        "known_cost_usd": "0.000082", "cost_complete": False,
    }
    assert _CANARY not in agent_log_capture.getvalue()
