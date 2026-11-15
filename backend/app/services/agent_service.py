import asyncio
import math
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.active_runs import ActiveExecution, ActiveRunRegistry
from app.services.agent_failure import AgentFailure, classify_execution_error
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import AgentResultContractError, encode_agent_result
from app.services.agent_runner import AgentRunner, AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunService


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60.0


def _check_interruption(handle: ActiveExecution, initial_cancelling: int) -> None:
    if handle.cancellation_requested or handle.task.cancelling() > initial_cancelling:
        raise asyncio.CancelledError()


class AgentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AgentRunner,
        *,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        active_runs: ActiveRunRegistry | None = None,
    ) -> None:
        if isinstance(execution_timeout_seconds, bool) or not isinstance(execution_timeout_seconds, (int, float)):
            raise ValueError("invalid_execution_timeout")
        try:
            timeout_seconds = float(execution_timeout_seconds)
        except OverflowError:
            raise ValueError("invalid_execution_timeout") from None
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid_execution_timeout")
        self._session_factory = session_factory
        self._runner = runner
        self._execution_timeout_seconds = timeout_seconds
        self._active_runs = active_runs if active_runs is not None else ActiveRunRegistry()

    async def execute_run(self, run_id: UUID) -> AgentFinalResult:
        handle = self._active_runs.register(run_id)
        try:
            request = await self._claim(run_id)
            initial_cancelling = handle.task.cancelling()
            _check_interruption(handle, initial_cancelling)
            try:
                handle.runner_active = True
                try:
                    result = await self._run_with_deadline(request)
                finally:
                    handle.runner_active = False
                _check_interruption(handle, initial_cancelling)
                try:
                    encode_agent_result(result)
                except AgentResultContractError:
                    raise AgentRunnerError("invalid_runner_result") from None
            except Exception as execution_error:
                _check_interruption(handle, initial_cancelling)
                failure = classify_execution_error(execution_error)
                try:
                    await self._fail(run_id, failure)
                except Exception as transition_error:
                    raise transition_error from execution_error
                raise

            await self._complete(run_id, result)
            return result
        finally:
            self._active_runs.unregister(run_id, handle)

    async def cancel_run(self, run_id: UUID) -> None:
        committed = False
        try:
            async with self._session_factory() as session:
                await AgentRunService(session).cancel_execution(run_id)
                committed = True
        finally:
            if committed:
                self._active_runs.request_cancel(run_id)

    async def _run_with_deadline(self, request: AgentRunRequest) -> AgentFinalResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._execution_timeout_seconds
        timeout = asyncio.timeout_at(deadline)
        try:
            async with timeout:
                result = await self._runner.run(request)
        except TimeoutError as exc:
            if timeout.expired():
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

    async def _claim(self, run_id: UUID) -> AgentRunRequest:
        async with self._session_factory() as session:
            return await AgentRunService(session).claim_for_execution(run_id)

    async def _complete(self, run_id: UUID, result: AgentFinalResult) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).complete_execution(run_id, result)

    async def _fail(self, run_id: UUID, failure: AgentFailure) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).fail_execution(run_id, failure)
