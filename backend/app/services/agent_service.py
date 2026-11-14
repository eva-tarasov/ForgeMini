from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution import AgentRunStatus
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import AgentResultContractError, encode_agent_result
from app.services.agent_runner import AgentRunner, AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunService


class AgentService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], runner: AgentRunner) -> None:
        self._session_factory = session_factory
        self._runner = runner

    async def execute_run(self, run_id: UUID) -> AgentFinalResult:
        request = await self._claim(run_id)
        try:
            result = await self._runner.run(request)
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
