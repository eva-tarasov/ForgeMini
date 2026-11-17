from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel, AgentRunModel
from app.domain.execution import AgentRunStatus, ApprovalStatus, ToolCallStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.approvals import ApprovalRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services.agent_failure import (
    AGENT_EXECUTION_FAILED_EVENT,
    AgentFailure,
    AgentFailureCode,
    AgentFailureContractError,
    decode_agent_failure,
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
from app.services.approval_contracts import ApprovalReference
from app.services.tasks import TaskNotFoundError
from app.services.tool_call_audit import ToolCallErrorCode


@dataclass(frozen=True, slots=True)
class _ApprovalTerminalFailure:
    reference: ApprovalReference
    code: ToolCallErrorCode


# The callback belongs to one AgentService invocation and is inherited by graph nodes.
_approval_failure_sink: ContextVar[Callable[[_ApprovalTerminalFailure], None] | None] = ContextVar(
    "approval_failure_sink", default=None,
)


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

    async def fail_execution(
        self, run_id: UUID, failure: AgentFailure, *, approval_failure: _ApprovalTerminalFailure | None = None,
    ) -> None:
        try:
            payload = encode_agent_failure(failure)
            run = await self.runs.get_for_update(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            actual = AgentRunStatus(run.status)
            settled = (
                actual in (AgentRunStatus.FAILED, AgentRunStatus.CANCELLED)
                and approval_failure is not None
                and await self._matches_approval_failure(run, failure, approval_failure)
            )
            if not settled:
                self._check_expected_status(run, AgentRunStatus.RUNNING)
            if run.final_result is not None:
                raise AgentRunResultAlreadyRecordedError("result_already_recorded")
            if actual is not AgentRunStatus.CANCELLED:
                existing = await self.events.get_execution_failure_events(run.id)
                if len(existing) > 1 or (existing and not settled):
                    raise AgentFailureContractError("duplicate_execution_failure")
                if not existing:
                    await self.events.append(run.id, event_type=AGENT_EXECUTION_FAILED_EVENT, payload=payload)
            if not settled:
                await self._transition_locked(run, AgentRunStatus.RUNNING, AgentRunStatus.FAILED)
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def _matches_approval_failure(
        self, run: AgentRunModel, failure: AgentFailure, evidence: _ApprovalTerminalFailure,
    ) -> bool:
        """Confirm a terminal outcome while holding the same parent lock as ApprovalGate."""
        reference, code = evidence.reference, evidence.code
        ticket = reference.ticket
        if ticket.run_id != run.id or ticket.tool_name != "apply_patch" or ticket.tool_version != 1:
            return False
        if run.status == AgentRunStatus.CANCELLED.value:
            if code is not ToolCallErrorCode.CANCELLED or failure.code is not AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED:
                return False
        elif code is ToolCallErrorCode.CANCELLED:
            # A committed rejection can win while the global deadline interrupts the waiter.
            if failure.code is not AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED:
                return False
            code = ToolCallErrorCode.APPROVAL_REJECTED

        status = ToolCallStatus.DENIED if code is ToolCallErrorCode.APPROVAL_REJECTED else ToolCallStatus.FAILED
        call = await ToolCallRepository(self.session).get(run.id, ticket.tool_call_id)
        if call is None or call.tool_name != ticket.tool_name or call.status != status.value:
            return False
        approvals = ApprovalRepository(self.session)
        approval = await approvals.get(reference.approval_id, ticket.tool_call_id)
        if approval is None or approval.decided_at is None:
            return False
        if approval.status not in (ApprovalStatus.REJECTED.value, ApprovalStatus.APPROVED.value):
            return False
        if code is ToolCallErrorCode.APPROVAL_REJECTED and approval.status != ApprovalStatus.REJECTED.value:
            return False
        if await approvals.event_payloads(run.id, reference.approval_id, "approval_requested") != [
            reference.requested_payload()
        ]:
            return False
        statement = select(AgentEventModel.payload).where(
            AgentEventModel.agent_run_id == run.id,
            AgentEventModel.event_type == "tool_call_finished",
            AgentEventModel.payload["tool_call_id"].astext == str(ticket.tool_call_id),
        ).limit(2)
        events = list((await self.session.scalars(statement)).all())
        return (
            len(events) == 1 and isinstance(events[0], dict)
            and events[0].get("status") == status.value and events[0].get("error_code") == code.value
        )

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

    async def get_usage(self, run_id: UUID) -> AgentUsageRecord | None:
        if await self.runs.get(run_id) is None:
            raise AgentRunNotFoundError(run_id)
        return await self._read_usage_event(run_id)

    async def _read_usage_event(self, run_id: UUID) -> AgentUsageRecord | None:
        events = await self.events.get_usage_events(run_id)
        if len(events) > 1:
            raise AgentUsageContractError("duplicate_agent_usage")
        if not events:
            return None
        return decode_agent_usage(run_id, events[0].payload)

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
