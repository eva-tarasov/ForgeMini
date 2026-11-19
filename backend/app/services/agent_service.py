import asyncio
import math
from collections.abc import Awaitable, Callable
from functools import partial
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.active_runs import ActiveExecution, ActiveRunRegistry
from app.services.agent_cost import AgentPricing
from app.services.agent_failure import AgentFailure, classify_execution_error
from app.services.agent_observability import AgentLogContext, AgentObserver, AgentOperation, AgentPhase
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import AgentResultContractError, encode_agent_result
from app.services.agent_runner import AgentRunner, AgentRunnerError, AgentRunRequest, ApprovalOutcomeRunner
from app.services.agent_runs import AgentRunService
from app.services.agent_usage import AgentUsageSnapshot, UsageCollector, capture_agent_usage
from app.services.agent_usage_codec import AgentUsageRecord
from app.services.approval_outcome import ApprovalOutcome, ApprovalOutcomeHolder


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60.0


class AgentUsagePersistenceError(RuntimeError):
    pass


def _check_interruption(handle: ActiveExecution, initial_cancelling: int) -> None:
    if handle.cancellation_requested or handle.task.cancelling() > initial_cancelling:
        raise asyncio.CancelledError()


async def cancel_agent_run(
    session_factory: Callable[[], AsyncSession], active_runs: ActiveRunRegistry, run_id: UUID,
) -> None:
    """Persist cancellation before signaling a local execution, even without a runner."""
    observer = AgentObserver(AgentLogContext(run_id, AgentOperation.CANCEL))
    with observer.operation():
        committed = False
        try:
            with observer.phase(AgentPhase.CANCELLATION_PERSISTENCE):
                async with session_factory() as session:
                    await AgentRunService(session).cancel_execution(run_id)
                    committed = True
        finally:
            if committed:
                with observer.phase(AgentPhase.CANCELLATION_SIGNAL):
                    active_runs.request_cancel(run_id)


class AgentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        runner: AgentRunner,
        *,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        active_runs: ActiveRunRegistry | None = None,
        pricing: AgentPricing | None = None,
    ) -> None:
        if isinstance(execution_timeout_seconds, bool) or not isinstance(execution_timeout_seconds, (int, float)):
            raise ValueError("invalid_execution_timeout")
        try:
            timeout_seconds = float(execution_timeout_seconds)
        except OverflowError:
            raise ValueError("invalid_execution_timeout") from None
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid_execution_timeout")
        if pricing is not None and not isinstance(pricing, AgentPricing):
            raise ValueError("invalid_agent_pricing")
        self._session_factory = session_factory
        self._runner = runner
        self._execution_timeout_seconds = timeout_seconds
        self._active_runs = active_runs if active_runs is not None else ActiveRunRegistry()
        self._pricing = pricing if pricing is not None else AgentPricing()

    async def execute_run(self, run_id: UUID) -> AgentFinalResult:
        observer = AgentObserver(AgentLogContext(run_id, AgentOperation.EXECUTE))
        with observer.operation():
            handle = self._active_runs.register(run_id)
            collector: UsageCollector | None = None
            primary_error: BaseException | None = None
            approval_failure: ApprovalOutcome | None = None

            try:
                with observer.phase(AgentPhase.CLAIM):
                    request = await self._claim(run_id)
                    observer.context.task_id = request.task_id
                initial_cancelling = handle.task.cancelling()
                _check_interruption(handle, initial_cancelling)
                try:
                    with capture_agent_usage(run_id) as collector:
                        with observer.phase(AgentPhase.RUNNER):
                            handle.runner_active = True
                            outcome_holder = None
                            try:
                                invoke = self._runner.run
                                if isinstance(self._runner, ApprovalOutcomeRunner):
                                    outcome_holder = ApprovalOutcomeHolder(run_id)
                                    invoke = partial(self._runner.run_with_approval_outcome, outcome=outcome_holder.port())
                                result = await self._run_with_deadline(request, invoke)
                            finally:
                                if outcome_holder is not None:
                                    approval_failure = outcome_holder.take_and_close()
                                handle.runner_active = False
                    _check_interruption(handle, initial_cancelling)
                    with observer.phase(AgentPhase.RESULT_VALIDATION):
                        try:
                            encode_agent_result(result)
                        except AgentResultContractError:
                            raise AgentRunnerError("invalid_runner_result") from None
                except Exception as execution_error:
                    _check_interruption(handle, initial_cancelling)
                    failure = classify_execution_error(execution_error)
                    observer.failure_classified(failure.code)
                    try:
                        with observer.phase(AgentPhase.FAILURE_PERSISTENCE):
                            if approval_failure is None:
                                await self._fail(run_id, failure)
                            else:
                                await self._fail(run_id, failure, approval_failure=approval_failure)
                    except Exception as transition_error:
                        if approval_failure is None or not approval_failure.interrupted_request:
                            raise transition_error from execution_error
                        # Неподтверждённый outcome не разрешает переход и не подменяет исходную ошибку.
                        execution_error.add_note("agent_failure_persistence_failed")
                    raise

                with observer.phase(AgentPhase.RESULT_PERSISTENCE):
                    await self._complete(run_id, result)
                return result
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    if collector is not None:
                        snapshot = collector.snapshot()
                        if snapshot.calls:
                            observer.usage_observed(snapshot, self._pricing)
                            with observer.phase(AgentPhase.USAGE_PERSISTENCE):
                                await self._persist_usage(snapshot)
                except Exception as write_error:
                    if primary_error is None:
                        raise AgentUsagePersistenceError("usage_persistence_failed") from write_error
                    primary_error.add_note("agent_usage_persistence_failed")
                finally:
                    self._active_runs.unregister(run_id, handle)

    async def cancel_run(self, run_id: UUID) -> None:
        await cancel_agent_run(self._session_factory, self._active_runs, run_id)

    async def _run_with_deadline(
        self, request: AgentRunRequest, invoke: Callable[[AgentRunRequest], Awaitable[AgentFinalResult]],
    ) -> AgentFinalResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._execution_timeout_seconds
        timeout = asyncio.timeout_at(deadline)
        try:
            async with timeout:
                result = await invoke(request)
        except Exception as exc:
            if timeout.expired() or loop.time() >= deadline:
                raise AgentRunnerError("execution_deadline_exceeded") from exc
            raise
        if timeout.expired() or loop.time() >= deadline:
            raise AgentRunnerError("execution_deadline_exceeded")
        return result

    async def get_run_result(self, run_id: UUID) -> AgentFinalResult | None:
        async with self._session_factory() as session:
            return await AgentRunService(session).get_final_result(run_id)

    async def get_run_failure(self, run_id: UUID) -> AgentFailure | None:
        async with self._session_factory() as session:
            return await AgentRunService(session).get_failure(run_id)

    async def get_run_usage(self, run_id: UUID) -> AgentUsageRecord | None:
        async with self._session_factory() as session:
            return await AgentRunService(session).get_usage(run_id)

    async def _persist_usage(self, snapshot: AgentUsageSnapshot) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).record_usage(AgentUsageRecord(snapshot, self._pricing))

    async def _claim(self, run_id: UUID) -> AgentRunRequest:
        async with self._session_factory() as session:
            return await AgentRunService(session).claim_for_execution(run_id)

    async def _complete(self, run_id: UUID, result: AgentFinalResult) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).complete_execution(run_id, result)

    async def _fail(
        self, run_id: UUID, failure: AgentFailure, *, approval_failure: ApprovalOutcome | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).fail_execution(run_id, failure, approval_failure=approval_failure)
