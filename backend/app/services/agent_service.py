from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution import AgentRunStatus
from app.services.agent_result import AgentFinalResult
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
            if not isinstance(result, AgentFinalResult):
                raise AgentRunnerError("invalid_runner_result")
            if result.status == "ready_for_verification":
                target = AgentRunStatus.VERIFYING
            elif result.status == "blocked":
                target = AgentRunStatus.FAILED
            else:
                raise AgentRunnerError("invalid_runner_result")
        except Exception as execution_error:
            try:
                await self._finish(run_id, AgentRunStatus.FAILED)
            except Exception as transition_error:
                raise transition_error from execution_error
            raise

        await self._finish(run_id, target)
        return result

    async def _claim(self, run_id: UUID) -> AgentRunRequest:
        async with self._session_factory() as session:
            return await AgentRunService(session).claim_for_execution(run_id)

    async def _finish(self, run_id: UUID, target: AgentRunStatus) -> None:
        async with self._session_factory() as session:
            await AgentRunService(session).transition_status(
                run_id,
                expected_status=AgentRunStatus.RUNNING,
                target_status=target,
            )
