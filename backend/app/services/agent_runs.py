from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel
from app.domain.execution import AgentRunStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_runner import AgentRunRequest
from app.services.tasks import TaskNotFoundError


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
        self.tasks = TaskRepository(session)

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
            await self._transition_locked(run, run_id, expected_status, target_status)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def claim_for_execution(self, run_id: UUID) -> AgentRunRequest:
        try:
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            self._check_expected_status(run, AgentRunStatus.QUEUED)
            task = await self.tasks.get(run.task_id)
            if task is None:
                raise TaskNotFoundError(run.task_id)
            request = AgentRunRequest(
                task_id=task.id,
                run_id=run.id,
                goal=task.goal,
                acceptance_criteria=task.acceptance_criteria,
            )
            await self._transition_locked(run, run_id, AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)
            await self.session.commit()
            return request
        except BaseException:
            await self.session.rollback()
            raise

    @staticmethod
    def _check_expected_status(run: AgentRunModel, expected: AgentRunStatus) -> AgentRunStatus:
        actual = AgentRunStatus(run.status)
        if actual != expected:
            raise StaleAgentRunStatusError(expected, actual)
        return actual

    async def _transition_locked(
        self, run: AgentRunModel, run_id: UUID, expected: AgentRunStatus, target: AgentRunStatus
    ) -> None:
        """Apply a transition and append its event under the parent row lock."""
        actual = self._check_expected_status(run, expected)
        validate_agent_run_transition(actual, target)
        if target is AgentRunStatus.SUCCEEDED:
            raise VerificationEvidenceRequiredError("Independent verification evidence is required")
        run.status = target.value
        await self.events.add_status_changed(run_id, actual, target)
