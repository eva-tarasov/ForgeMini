from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel
from app.domain.execution import AgentRunStatus
from app.domain.task import TaskStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_failure import (
    AGENT_EXECUTION_FAILED_EVENT,
    AgentFailure,
    AgentFailureContractError,
    encode_agent_failure,
)
from app.services.agent_result import AgentFinalResult
from app.services.agent_result_codec import decode_agent_result, encode_agent_result
from app.services.agent_runner import AgentRunRequest
from app.services.agent_usage_codec import (
    AGENT_USAGE_RECORDED_EVENT,
    AgentUsageContractError,
    AgentUsageRecord,
    decode_agent_usage,
    encode_agent_usage,
)
from app.services.tasks import TaskNotFoundError


class AgentRunNotFoundError(Exception):
    pass


class AgentRunCreateConflictError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AgentRunMetadata:
    id: UUID
    task_id: UUID
    attempt_number: int
    status: AgentRunStatus
    created_at: datetime

    @classmethod
    def from_record(cls, run: AgentRunModel) -> "AgentRunMetadata":
        return cls(run.id, run.task_id, run.attempt_number, AgentRunStatus(run.status), run.created_at)


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


def _check_expected_status(run: AgentRunModel, expected: AgentRunStatus) -> AgentRunStatus:
    actual = AgentRunStatus(run.status)
    if actual != expected:
        raise StaleAgentRunStatusError(expected, actual)
    return actual


async def transition_locked_agent_run(
    events: AgentEventRepository, locked_run: AgentRunModel, expected: AgentRunStatus, target: AgentRunStatus,
) -> None:
    """Stage a transition using the repository bound to the session holding the row lock."""
    actual = _check_expected_status(locked_run, expected)
    validate_agent_run_transition(actual, target)
    if target is AgentRunStatus.SUCCEEDED:
        raise VerificationEvidenceRequiredError("Independent verification evidence is required")
    if target is AgentRunStatus.VERIFYING:
        if locked_run.final_result is None:
            raise AgentRunResultRequiredError("final_result_required")
        if decode_agent_result(locked_run.final_result).status != "ready_for_verification":
            raise AgentRunResultRequiredError("result_not_ready_for_verification")
    locked_run.status = target.value
    await events.add_status_changed(locked_run.id, actual, target)


class AgentRunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.runs = AgentRunRepository(session)
        self.events = AgentEventRepository(session)
        self.tasks = TaskRepository(session)

    async def create_initial_run(self, task_id: UUID) -> tuple[AgentRunMetadata, bool]:
        """Create attempt one or replay it if it is the only run and still queued."""
        try:
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            existing = await self.runs.list_for_task(task_id, limit=2)
            if existing:
                if (
                    len(existing) != 1
                    or existing[0].attempt_number != 1
                    or existing[0].status != AgentRunStatus.QUEUED.value
                ):
                    raise AgentRunCreateConflictError()
                run = existing[0]
            else:
                # Read the locked status even if the caller previously loaded this Task.
                await self.session.refresh(task, attribute_names=["status"])
                if task.status != TaskStatus.QUEUED.value:
                    raise AgentRunCreateConflictError()
                run = await self.runs.add_initial(task_id)
            metadata = AgentRunMetadata.from_record(run)
            await self.session.commit()
            return metadata, not existing
        except BaseException as error:
            try:
                await self.session.rollback()
            except Exception:
                error.add_note("agent_run_create_rollback_failed")
            raise

    async def get_metadata(self, run_id: UUID) -> AgentRunMetadata:
        run = await self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return AgentRunMetadata.from_record(run)

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
            await transition_locked_agent_run(self.events, run, expected_status, target_status)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def claim_for_execution(self, run_id: UUID) -> AgentRunRequest:
        try:
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            _check_expected_status(run, AgentRunStatus.QUEUED)
            task = await self.tasks.get(run.task_id)
            if task is None:
                raise TaskNotFoundError(run.task_id)
            request = AgentRunRequest(
                task_id=task.id,
                run_id=run.id,
                goal=task.goal,
                acceptance_criteria=task.acceptance_criteria,
            )
            await transition_locked_agent_run(self.events, run, AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)
            await self.session.commit()
            return request
        except BaseException:
            await self.session.rollback()
            raise

    async def cancel_execution(self, run_id: UUID) -> None:
        try:
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            actual = AgentRunStatus(run.status)
            if actual is not AgentRunStatus.CANCELLED:
                await transition_locked_agent_run(self.events, run, actual, AgentRunStatus.CANCELLED)
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
            _check_expected_status(run, AgentRunStatus.RUNNING)
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
            await transition_locked_agent_run(self.events, run, AgentRunStatus.RUNNING, target)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def fail_execution(self, run_id: UUID, failure: AgentFailure) -> None:
        try:
            payload = encode_agent_failure(failure)
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            _check_expected_status(run, AgentRunStatus.RUNNING)
            if run.final_result is not None:
                raise AgentRunResultAlreadyRecordedError("result_already_recorded")
            existing = await self.events.get_execution_failure_events(run.id)
            if existing:
                raise AgentFailureContractError("duplicate_execution_failure")
            await self.events.append(run.id, event_type=AGENT_EXECUTION_FAILED_EVENT, payload=payload)
            await transition_locked_agent_run(self.events, run, AgentRunStatus.RUNNING, AgentRunStatus.FAILED)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def record_usage(self, record: AgentUsageRecord) -> None:
        try:
            payload = encode_agent_usage(record)
            run_id = record.snapshot.run_id
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            if AgentRunStatus(run.status) is AgentRunStatus.QUEUED:
                raise AgentUsageContractError("usage_requires_claimed_run")
            existing = await self._read_usage_event(run_id)
            if existing is None:
                await self.events.append(run_id, event_type=AGENT_USAGE_RECORDED_EVENT, payload=payload)
            elif existing != record:
                raise AgentUsageContractError("conflicting_agent_usage")
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def _read_usage_event(self, run_id: UUID) -> AgentUsageRecord | None:
        events = await self.events.get_usage_events(run_id)
        if len(events) > 1:
            raise AgentUsageContractError("duplicate_agent_usage")
        if not events:
            return None
        return decode_agent_usage(run_id, events[0].payload)
