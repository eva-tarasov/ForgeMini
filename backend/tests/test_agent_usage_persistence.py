import asyncio
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.active_runs import ActiveRunRegistry
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import StaleAgentRunStatusError
from app.services.agent_service import AgentService, AgentUsagePersistenceError
from app.services.agent_usage import (
    AgentUsageSnapshot,
    ModelCallOutcome as Outcome,
    ModelCallStage as Stage,
    ModelCallUsage,
    TokenUsage,
    UsageStatus as Status,
    capture_agent_usage,
    current_usage_collector,
)
from app.services.agent_usage_codec import AgentUsageContractError, AgentUsageRecord, decode_agent_usage, encode_agent_usage
from app.domain.execution import AgentRunStatus


TOKENS = TokenUsage(12, 8, 20, 4, 2)
READY = AgentFinalResult(
    summary="Synthetic analysis", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)


@pytest.fixture
def record():
    tariff = ModelTariff("openai", "synthetic-model", "training-v1", Decimal("2.00"), Decimal("8.00"), Decimal("0.50"))
    return AgentUsageRecord(AgentUsageSnapshot(uuid4(), (
        ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, TOKENS),
        ModelCallUsage(2, Stage.FINALIZATION, Outcome.CANCELLED, Status.MISSING, None),
    )), AgentPricing(research=tariff))


def test_usage_codec_round_trip_keeps_exact_historical_pricing(record):
    payload = encode_agent_usage(record)
    assert payload == {
        "schema_version": 1,
        "calls": [
            {"call_number": 1, "stage": "research", "outcome": "returned", "usage_status": "reported", "tokens": {
                "input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "cached_input_tokens": 4, "reasoning_tokens": 2,
            }},
            {"call_number": 2, "stage": "finalization", "outcome": "cancelled", "usage_status": "missing", "tokens": None},
        ],
        "pricing": {
            "research": {"provider": "openai", "model": "synthetic-model", "tariff_version": "training-v1",
                         "input_usd_per_million": "2.00", "output_usd_per_million": "8.00",
                         "cached_input_usd_per_million": "0.50"},
            "finalization": None,
        },
    }
    stored = decode_agent_usage(record.snapshot.run_id, json.loads(json.dumps(payload)))
    assert stored == record
    assert type(stored.snapshot.calls) is tuple and stored.snapshot.calls[0].stage is Stage.RESEARCH
    assert stored.pricing.research.input_usd_per_million.as_tuple() == Decimal("2.00").as_tuple()
    assert stored.cost_estimate.known_cost_usd == Decimal("0.000082")
    assert not stored.cost_estimate.cost_complete
    with pytest.raises(FrozenInstanceError):
        stored.pricing = AgentPricing()


_MISSING = object()


@pytest.mark.parametrize("path,value", [
    (("pricing",), _MISSING), (("run_id",), "extra"), (("calls",), []), (("calls",), ()),
    (("pricing", "finalization"), _MISSING), (("pricing", "other"), None),
    (("calls", 0, "stage"), _MISSING), (("calls", 0, "extra"), "private"),
    (("calls", 0, "stage"), "unknown"), (("calls", 0, "outcome"), "unknown"),
    (("calls", 0, "outcome"), "error"), (("calls", 0, "usage_status"), "unknown"),
    (("calls", 0, "usage_status"), "invalid"), (("calls", 0, "call_number"), True),
    (("calls", 1, "call_number"), 1), (("calls", 0, "tokens"), None),
    (("calls", 0, "tokens", "reasoning_tokens"), _MISSING),
    (("calls", 0, "tokens", "extra"), 0), (("calls", 0, "tokens", "input_tokens"), 12.0),
    (("calls", 0, "tokens", "total_tokens"), 100), (("calls", 0, "tokens", "cached_input_tokens"), 13),
    (("pricing", "research", "provider"), _MISSING), (("pricing", "research", "extra"), True),
    (("pricing", "research", "model"), "free text"),
    (("pricing", "research", "input_usd_per_million"), 2),
    (("pricing", "research", "input_usd_per_million"), "2e0"),
    (("pricing", "research", "input_usd_per_million"), "2.0000001"),
    (("pricing", "research", "input_usd_per_million"), "1000000.000001"),
    (("pricing", "research", "input_usd_per_million"), "NaN"),
    (("pricing", "research", "input_usd_per_million"), None),
    (("schema_version",), True), (("schema_version",), 1.0), (("schema_version",), "1"),
])
def test_usage_codec_rejects_invalid_document(record, path, value):
    payload = encode_agent_usage(record)
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is _MISSING:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    with pytest.raises(AgentUsageContractError, match="^invalid_stored_usage$"):
        decode_agent_usage(record.snapshot.run_id, payload)


def test_usage_codec_rejects_unknown_version_and_empty_record(record):
    with pytest.raises(AgentUsageContractError, match="^unsupported_usage_version$"):
        decode_agent_usage(record.snapshot.run_id, {**encode_agent_usage(record), "schema_version": 2})
    with pytest.raises(AgentUsageContractError, match="^invalid_agent_usage$"):
        AgentUsageRecord(AgentUsageSnapshot(record.snapshot.run_id, ()), record.pricing)
    with pytest.raises(AgentUsageContractError, match="^invalid_agent_usage$"):
        encode_agent_usage({})


def _observe_usage():
    collector = current_usage_collector()
    number = collector.start_call(Stage.RESEARCH)
    collector.finish_call(number, outcome=Outcome.RETURNED, usage_status=Status.REPORTED, tokens=TOKENS)
    return collector


@pytest.fixture
def make_service(monkeypatch):
    def make(run, run_id, **configuration):
        request = AgentRunRequest(uuid4(), run_id, "Synthetic task", ())
        service = AgentService(None, SimpleNamespace(run=run), **configuration)
        monkeypatch.setattr(service, "_claim", AsyncMock(return_value=request))
        monkeypatch.setattr(service, "_complete", AsyncMock())
        monkeypatch.setattr(service, "_fail", AsyncMock())
        monkeypatch.setattr(service, "_persist_usage", AsyncMock())
        return service, request

    return make


def test_service_owns_scope_only_during_runner_and_restores_caller(make_service):
    async def check():
        run_id = uuid4()
        entered = []

        async def run(_request):
            collector = _observe_usage()
            assert collector is not outer and collector.snapshot().run_id == run_id
            entered.append(("runner", collector))
            return READY

        async def claim(_run_id):
            assert current_usage_collector() is outer
            entered.append(("claim", None))
            return request

        async def complete(_run_id, _result):
            assert current_usage_collector() is outer
            assert entered[1][1].closed
            entered.append(("completion", None))

        async def persist(snapshot):
            assert current_usage_collector() is outer and run_id in service._active_runs
            assert snapshot == entered[1][1].snapshot()
            entered.append(("usage", None))

        service, request = make_service(run, run_id)
        service._claim.side_effect = claim
        service._complete.side_effect = complete
        service._persist_usage.side_effect = persist
        with capture_agent_usage(uuid4()) as outer:
            assert await service.execute_run(run_id) is READY
            assert current_usage_collector() is outer and outer.snapshot().calls == ()
        assert current_usage_collector() is None
        assert [name for name, _ in entered] == ["claim", "runner", "completion", "usage"]
        assert run_id not in service._active_runs
        service._persist_usage.assert_awaited_once()
        service._fail.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("primary", ["success", "runner-error", "completion-error", "cancellation"])
def test_usage_persistence_errors_preserve_primary_identity_and_cause(make_service, primary):
    async def check():
        run_id = uuid4()
        started = asyncio.Event()
        root = RuntimeError("synthetic root")
        execution_error = AgentRunnerError("execution_failed")
        completion_error = RuntimeError("synthetic completion failure")
        write_error = RuntimeError("synthetic usage write failure")
        original_cancellation = []
        propagated_cancellation = []

        async def run(_request):
            _observe_usage()
            if primary == "runner-error":
                raise execution_error from root
            if primary == "cancellation":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    original_cancellation.append(error)
                    raise
            return READY

        service, _ = make_service(run, run_id)
        service._persist_usage.side_effect = write_error
        if primary == "completion-error":
            service._complete.side_effect = completion_error

        async def execute():
            try:
                return await service.execute_run(run_id)
            except asyncio.CancelledError as error:
                propagated_cancellation.append(error)
                raise

        async with asyncio.timeout(5):
            if primary == "cancellation":
                task = asyncio.create_task(execute())
                try:
                    await started.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
                assert len(original_cancellation) == 1
                assert propagated_cancellation[0] is original_cancellation[0]
                error = propagated_cancellation[0]
                assert error.__cause__ is None
            elif primary == "success":
                with pytest.raises(AgentUsagePersistenceError, match="^usage_persistence_failed$") as exc:
                    await execute()
                assert exc.value.__cause__ is write_error
            else:
                with pytest.raises(RuntimeError) as exc:
                    await execute()
                error = exc.value
                assert error is (execution_error if primary == "runner-error" else completion_error)
                assert error.__cause__ is (root if primary == "runner-error" else None)
        if primary != "success":
            assert error.__notes__ == ["agent_usage_persistence_failed"]
        if primary == "runner-error":
            service._fail.assert_awaited_once()
        else:
            service._fail.assert_not_called()
        service._persist_usage.assert_awaited_once()
        assert run_id not in service._active_runs and current_usage_collector() is None

    asyncio.run(check())


@pytest.mark.parametrize("case", ["claim-loser", "pre-run-cancel", "empty-runner"])
def test_service_does_not_persist_unobserved_calls(make_service, case):
    async def check():
        run_id = uuid4()
        invoked = []

        async def run(_request):
            invoked.append(True)
            return READY

        registry = ActiveRunRegistry()
        service, request = make_service(run, run_id, active_runs=registry)
        if case == "claim-loser":
            service._claim.side_effect = StaleAgentRunStatusError(AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)
            with pytest.raises(StaleAgentRunStatusError):
                await service.execute_run(run_id)
        elif case == "pre-run-cancel":
            async def claim(_run_id):
                registry.request_cancel(run_id)
                return request

            service._claim.side_effect = claim
            with pytest.raises(asyncio.CancelledError):
                await service.execute_run(run_id)
        else:
            assert await service.execute_run(run_id) is READY
        assert bool(invoked) is (case == "empty-runner")
        service._persist_usage.assert_not_called()
        service._fail.assert_not_called()
        assert run_id not in registry and current_usage_collector() is None

    asyncio.run(check())


def test_service_rejects_invalid_pricing():
    with pytest.raises(ValueError, match="^invalid_agent_pricing$"):
        AgentService(None, None, pricing={})


def test_usage_write_is_outside_local_runner_cancellation(make_service):
    async def check():
        run_id = uuid4()
        writing, release = asyncio.Event(), asyncio.Event()
        registry = ActiveRunRegistry()

        async def run(_request):
            _observe_usage()
            return READY

        async def persist(_snapshot):
            writing.set()
            await release.wait()

        service, _ = make_service(run, run_id, active_runs=registry, execution_timeout_seconds=0.5)
        service._persist_usage.side_effect = persist
        task = asyncio.create_task(service.execute_run(run_id))
        try:
            async with asyncio.timeout(5):
                await writing.wait()
                assert run_id in registry
                registry.request_cancel(run_id)
                assert task.cancelling() == 0
                release.set()
                assert await task is READY
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        assert run_id not in registry

    asyncio.run(check())
