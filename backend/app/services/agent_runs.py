from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import AgentRunStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository


class AgentRunNotFoundError(Exception):
    pass


class StaleAgentRunStatusError(Exception):
    def __init__(self, expected: AgentRunStatus, actual: AgentRunStatus) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Expected {expected.value}, found {actual.value}")


class VerificationEvidenceRequiredError(Exception):
    pass


class AgentRunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.runs = AgentRunRepository(session)
        self.events = AgentEventRepository(session)

    async def transition_status(
        self, run_id: UUID, expected_status: AgentRunStatus, target_status: AgentRunStatus
    ) -> None:
        try:
            if not isinstance(expected_status, AgentRunStatus):
                raise TypeError("expected_status must be an AgentRunStatus")
            if not isinstance(target_status, AgentRunStatus):
                raise TypeError("target_status must be an AgentRunStatus")
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            actual = AgentRunStatus(run.status)
            if actual != expected_status:
                raise StaleAgentRunStatusError(expected_status, actual)
            validate_agent_run_transition(actual, target_status)
            if target_status is AgentRunStatus.SUCCEEDED:
                raise VerificationEvidenceRequiredError("Independent verification evidence is required")
            run.status = target_status.value
            await self.events.add_status_changed(run_id, actual, target_status)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
