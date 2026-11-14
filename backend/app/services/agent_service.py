import asyncio
import math
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution import AgentRunStatus
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import AgentResultContractError, encode_agent_result
from app.services.agent_runner import AgentRunner, AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunService


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60.0


class AgentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AgentRunner,
        *,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
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

    async def execute_run(self, run_id: UUID) -> AgentFinalResult:
        request = await self._claim(run_id)
        try:
            result = await self._run_with_deadline(request)
            try:
                encode_agent_result(result)
            except AgentResultContractError:
                raise AgentRunnerError("invalid_runner_result") from None
        except Exception as execution_error:
            try:
                await self._fail(run_id)
            except Exception as transition_error:
                raise transition_error from execution_error
            raise

        await self._complete(run_id, result)
        return result

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

    async def _claim(self, run_id: UUID) -> AgentRunRequest:
        async with self._session_factory() as session:
            return await AgentRunService(session).claim_for_execution(run_id)

    async def _complete(self, run_id: UUID, result: AgentFinalResult) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).complete_execution(run_id, result)

    async def _fail(self, run_id: UUID) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).transition_status(
                run_id,
                expected_status=AgentRunStatus.RUNNING,
                target_status=AgentRunStatus.FAILED,
            )
