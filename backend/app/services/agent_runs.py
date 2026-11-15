from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel
from app.domain.execution import AgentRunStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_failure import (
    AGENT_EXECUTION_FAILED_EVENT,
    AgentFailure,
    AgentFailureContractError,
    decode_agent_failure,
    encode_agent_failure,
)
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import decode_agent_result, encode_agent_result
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


class AgentRunResultAlreadyRecordedError(Exception):
    pass


class AgentRunResultRequiredError(Exception):
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
            await self._transition_locked(run, expected_status, target_status)
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
            await self._transition_locked(run, AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)
            await self.session.commit()
            return request
        except BaseException:
            await self.session.rollback()
            raise

    async def get_final_result(self, run_id: UUID) -> AgentFinalResult | None:
        run = await self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        if run.final_result is None:
            return None
        return decode_agent_result(run.final_result)

    async def cancel_execution(self, run_id: UUID) -> None:
        try:
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            actual = AgentRunStatus(run.status)
            if actual is not AgentRunStatus.CANCELLED:
                await self._transition_locked(run, actual, AgentRunStatus.CANCELLED)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def complete_execution(self, run_id: UUID, result: AgentFinalResult) -> None:
        try:
            document = encode_agent_result(result)
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            self._check_expected_status(run, AgentRunStatus.RUNNING)
            if run.final_result is not None:
                raise AgentRunResultAlreadyRecordedError("result_already_recorded")
            result_status = document["result"]["status"]
            target = AgentRunStatus.VERIFYING if result_status == "ready_for_verification" else AgentRunStatus.FAILED
            run.final_result = document
            await self.events.append(
                run.id,
                event_type="agent_result_recorded",
                payload={"schema_version": document["schema_version"], "result_status": result_status},
            )
            await self._transition_locked(run, AgentRunStatus.RUNNING, target)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def get_failure(self, run_id: UUID) -> AgentFailure | None:
        if await self.runs.get(run_id) is None:
            raise AgentRunNotFoundError(run_id)
        events = await self.events.get_execution_failure_events(run_id)
        if not events:
            return None
        if len(events) > 1:
            raise AgentFailureContractError("duplicate_execution_failure")
        return decode_agent_failure(events[0].payload)

    async def fail_execution(self, run_id: UUID, failure: AgentFailure) -> None:
        try:
            payload = encode_agent_failure(failure)
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            self._check_expected_status(run, AgentRunStatus.RUNNING)
            if run.final_result is not None:
                raise AgentRunResultAlreadyRecordedError("result_already_recorded")
            if await self.events.get_execution_failure_events(run.id):
                raise AgentFailureContractError("duplicate_execution_failure")
            await self.events.append(run.id, event_type=AGENT_EXECUTION_FAILED_EVENT, payload=payload)
            await self._transition_locked(run, AgentRunStatus.RUNNING, AgentRunStatus.FAILED)
            await self.session.commit()
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
        self, run: AgentRunModel, expected: AgentRunStatus, target: AgentRunStatus
    ) -> None:
        """Apply a transition and append its event under the parent row lock."""
        actual = self._check_expected_status(run, expected)
        validate_agent_run_transition(actual, target)
        if target is AgentRunStatus.SUCCEEDED:
            raise VerificationEvidenceRequiredError("Independent verification evidence is required")
        if target is AgentRunStatus.VERIFYING:
            if run.final_result is None:
                raise AgentRunResultRequiredError("final_result_required")
            if decode_agent_result(run.final_result).status != "ready_for_verification":
                raise AgentRunResultRequiredError("result_not_ready_for_verification")
        run.status = target.value
        await self.events.add_status_changed(run.id, actual, target)
