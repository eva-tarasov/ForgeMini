"""Contract of AgentRunService / AgentService error paths against real PostgreSQL.

Ported in ADR-003 (C4). These branches used to be exercised only through the LangGraph runner tests
(test_agent_service_integration & co.). The behaviour is the kernel's, so it is tested with plain runner doubles:
- every write method fails closed on a missing run, a wrong status or contradictory storage, and rolls back;
- usage is written once, an identical retry is a no-op, a different second record is a conflict;
- if persisting an execution failure itself fails, the caller sees THAT error with the original one as its cause.
Opt-in like the other task06 integration tests (FORGE_TASK06_TEST_DB + POSTGRES_DB).
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import AgentEventModel, AgentRunModel
from app.domain.execution import AgentRunStatus
from app.repositories.agent_events import AgentEventRepository
from app.services.agent_failure import (
    AGENT_EXECUTION_FAILED_EVENT, AgentFailure, AgentFailureCode, AgentFailureContractError,
)
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import encode_agent_result
from app.services.agent_runner import AgentRunnerError
from app.services.agent_runs import (
    AgentRunNotFoundError, AgentRunResultAlreadyRecordedError, AgentRunService, StaleAgentRunStatusError,
)
from app.services.agent_service import AgentService
from app.services.agent_usage import (
    AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, TokenUsage, UsageStatus,
)
from app.services.agent_usage_codec import AgentUsageContractError, AgentUsageRecord, encode_agent_usage
from app.services.agent_cost import AgentPricing
from run_harness import make_run_scenario

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to the dedicated forge_task06_* database",
)
READY = AgentFinalResult(
    summary="Synthetic result", status="ready_for_verification", relevant_components=(), assumptions=(),
    observations=(), blocked_reason=None,
)
FAILURE = AgentFailure(AgentFailureCode.EXECUTION_FAILED)


@pytest.fixture
def run_scenario():
    return make_run_scenario()


def _usage(run_id, total=2):
    call = ModelCallUsage(1, ModelCallStage.RESEARCH, ModelCallOutcome.RETURNED, UsageStatus.REPORTED,
                          TokenUsage(1, total - 1, total))
    return AgentUsageRecord(AgentUsageSnapshot(run_id, (call,)), AgentPricing())


async def _state(case):
    """(status, final_result, number of events) as committed, through an independent session."""
    async with case.factory() as session:
        run = await session.get(AgentRunModel, case.run_id)
        count = await session.scalar(
            select(func.count()).select_from(AgentEventModel).where(AgentEventModel.agent_run_id == case.run_id))
        return run.status, run.final_result, count


async def _claimed(case):
    async with case.factory() as session:
        await AgentRunService(session).claim_for_execution(case.run_id)


def test_every_write_rejects_a_missing_run(run_scenario):
    async def check(case):
        missing = uuid4()
        async with case.factory() as session:
            service = AgentRunService(session)
            for call in (
                lambda: service.claim_for_execution(missing),
                lambda: service.cancel_execution(missing),
                lambda: service.complete_execution(missing, READY),
                lambda: service.fail_execution(missing, FAILURE),
                lambda: service.record_usage(_usage(missing)),
            ):
                with pytest.raises(AgentRunNotFoundError):
                    await call()
        assert await _state(case) == ("queued", None, 0)        # the real run is untouched
    run_scenario(check)


def test_result_and_failure_are_refused_once_a_result_is_stored(run_scenario):
    """Contradictory storage (RUNNING with a stored result) must not get a second result or a failure on top."""
    async def check(case):
        await _claimed(case)
        async with case.factory.begin() as session:
            (await session.get(AgentRunModel, case.run_id)).final_result = encode_agent_result(READY)
        async with case.factory() as session:
            service = AgentRunService(session)
            with pytest.raises(AgentRunResultAlreadyRecordedError):
                await service.complete_execution(case.run_id, READY)
            with pytest.raises(AgentRunResultAlreadyRecordedError):
                await service.fail_execution(case.run_id, FAILURE)
        status, _, events = await _state(case)
        assert (status, events) == ("running", 1)                 # only queued -> running; nothing appended
    run_scenario(check)


def test_second_execution_failure_is_refused_and_rolled_back(run_scenario):
    async def check(case):
        await _claimed(case)
        async with case.factory.begin() as session:      # a failure event exists but the status was not moved
            await AgentEventRepository(session).append(
                case.run_id, event_type=AGENT_EXECUTION_FAILED_EVENT, payload={"schema_version": 1, "code": "execution_failed"})
        async with case.factory() as session:
            with pytest.raises(AgentFailureContractError):
                await AgentRunService(session).fail_execution(case.run_id, FAILURE)
        assert await _state(case) == ("running", None, 2)
    run_scenario(check)


def test_failure_on_a_run_that_is_not_running_is_refused(run_scenario):
    async def check(case):
        async with case.factory() as session:
            with pytest.raises(StaleAgentRunStatusError):
                await AgentRunService(session).fail_execution(case.run_id, FAILURE)
        assert await _state(case) == ("queued", None, 0)
    run_scenario(check)


def test_usage_is_written_once_identical_retry_is_a_no_op_and_a_different_one_conflicts(run_scenario):
    async def check(case):
        async with case.factory() as session:
            with pytest.raises(AgentUsageContractError, match="usage_requires_claimed_run"):
                await AgentRunService(session).record_usage(_usage(case.run_id))
        await _claimed(case)
        async with case.factory() as session:
            service = AgentRunService(session)
            await service.record_usage(_usage(case.run_id))
            await service.record_usage(_usage(case.run_id))                  # idempotent retry
            with pytest.raises(AgentUsageContractError, match="conflicting_agent_usage"):
                await service.record_usage(_usage(case.run_id, total=3))
        assert await _state(case) == ("running", None, 2)                   # status change + ONE usage event
    run_scenario(check)


def test_duplicate_stored_usage_events_fail_closed(run_scenario):
    async def check(case):
        await _claimed(case)
        payload = encode_agent_usage(_usage(case.run_id))
        async with case.factory.begin() as session:
            for _ in range(2):
                await AgentEventRepository(session).append(case.run_id, event_type="agent_usage_recorded", payload=payload)
        async with case.factory() as session:
            with pytest.raises(AgentUsageContractError, match="duplicate_agent_usage"):
                await AgentRunService(session).record_usage(_usage(case.run_id))
    run_scenario(check)


def test_failure_persistence_error_surfaces_with_the_execution_error_as_cause(run_scenario, monkeypatch):
    """If the run cannot even be marked FAILED, the caller must see that (the run is stuck in RUNNING), and the
    original execution error must not be lost."""
    async def check(case):
        class Broken:
            async def run(self, request):
                raise AgentRunnerError("synthetic_runner_failure")

        async def cannot_persist(self, run_id, failure):
            raise RuntimeError("synthetic database outage")

        monkeypatch.setattr(AgentRunService, "fail_execution", cannot_persist)
        with pytest.raises(RuntimeError, match="synthetic database outage") as raised:
            await AgentService(case.factory, Broken()).execute_run(case.run_id)
        assert isinstance(raised.value.__cause__, AgentRunnerError)
        status, _, _ = await _state(case)
        assert status == AgentRunStatus.RUNNING.value
    run_scenario(check)


def test_model_calls_reported_by_the_runner_are_persisted_once_as_usage(run_scenario):
    """The kernel contract a runner uses to report model calls: current_usage_collector() inside execute_run.
    NOTE (C4 finding): the sandbox loop does not use it yet — it counts tokens for the sandbox report only."""
    from app.services.agent_usage import current_usage_collector
    from app.services.agent_usage_codec import decode_agent_usage

    async def check(case):
        class Reporting:
            async def run(self, request):
                collector = current_usage_collector()
                number = collector.start_call(ModelCallStage.RESEARCH)
                collector.finish_call(number, outcome=ModelCallOutcome.RETURNED, usage_status=UsageStatus.REPORTED,
                                      tokens=TokenUsage(3, 2, 5))
                return READY

        assert await AgentService(case.factory, Reporting()).execute_run(case.run_id) == READY
        async with case.factory() as session:
            [event] = await AgentEventRepository(session).get_usage_events(case.run_id)
        usage = decode_agent_usage(case.run_id, event.payload)
        assert (usage.snapshot.known_total_tokens, usage.snapshot.usage_complete) == (5, True)
        assert current_usage_collector() is None                # the collector is scoped to that one execution
        status, _, _ = await _state(case)
        assert status == "verifying"
    run_scenario(check)
