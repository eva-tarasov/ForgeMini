import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentRunModel, ApprovalModel, ToolCallModel
from app.domain.execution import AgentRunStatus, ApprovalStatus, ToolCallStatus
from app.permissions.context import PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.approvals import ApprovalRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services.agent_runs import transition_locked_agent_run
from app.services.approval_contracts import (
    ApprovalDecision,
    ApprovalReference,
    PendingOperation,
    validate_patch_proposal,
)
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallErrorCode
from app.services.tool_calls import (
    lock_proposed_tool_call,
    lock_tool_call_run,
    stage_tool_call_proposal,
    tool_call_transaction,
    transition_locked_tool_call,
)
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor


APPROVAL_REQUESTED = "approval_requested"
APPROVAL_DECIDED = "approval_decided"


class ApprovalGate:
    """Short approval transactions. The caller owns the live coroutine and its slot."""

    def __init__(
        self, executor: ToolExecutor, session_factory: async_sessionmaker[AsyncSession], pending: PendingApprovals,
    ) -> None:
        self._executor = executor
        self._session_factory = session_factory
        self._pending = pending

    def _check_binding(self, operation: PendingOperation) -> None:
        operation.check_identity()
        ticket = operation.reference.ticket
        tool, _, writer = validate_patch_proposal(
            self._executor, ticket.tool_name, {"patch": operation.patch},
            version=ticket.tool_version, context=operation.context,
        )
        if tool is not operation.tool or writer is not operation.writer:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    @staticmethod
    def check_deadline(operation: PendingOperation) -> None:
        if asyncio.get_running_loop().time() >= operation.deadline:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_TIMEOUT)

    async def request(self, operation: PendingOperation) -> None:
        self._pending.require_reserved(operation)
        reference, ticket = operation.reference, operation.reference.ticket
        async with tool_call_transaction(self._session_factory) as session:
            run = await lock_tool_call_run(session, ticket, require_running=True)
            self._pending.require_reserved(operation)
            self._check_binding(operation)
            self.check_deadline(operation)
            repository = ApprovalRepository(session)
            if await repository.has_active(ticket.run_id):
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            await stage_tool_call_proposal(session, ticket)
            repository.add_pending(reference.approval_id, ticket.tool_call_id)
            await AgentEventRepository(session).append(
                ticket.run_id, event_type=APPROVAL_REQUESTED, payload=reference.requested_payload(),
            )
            await transition_locked_agent_run(
                AgentEventRepository(session), run, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_APPROVAL,
            )
        self._pending.publish(operation)

    async def _reconcile_unpublished_request(self, operation: PendingOperation, code: ToolCallErrorCode) -> None:
        self._pending.require_reserved(operation)
        reference, ticket = operation.reference, operation.reference.ticket
        async with tool_call_transaction(self._session_factory) as session:
            run = await lock_tool_call_run(session, ticket, require_running=False)
            call = await lock_proposed_tool_call(session, ticket)
            if call is None:
                return
            approvals = ApprovalRepository(session)
            approval = await approvals.get_for_update(reference.approval_id, ticket.tool_call_id)
            self._pending.require_reserved(operation)
            if (
                approval is None or approval.status != ApprovalStatus.PENDING.value or approval.decided_at is not None
                or await approvals.event_payloads(ticket.run_id, reference.approval_id, APPROVAL_REQUESTED)
                != [reference.requested_payload()]
                or await approvals.event_payloads(ticket.run_id, reference.approval_id, APPROVAL_DECIDED)
            ):
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            await self._record_decision(session, reference, approval, ApprovalStatus.REJECTED, reason=code)
            await transition_locked_tool_call(session, ticket, call, ToolCallStatus.FAILED, error_code=code)
            if run.status == AgentRunStatus.WAITING_APPROVAL.value:
                target = AgentRunStatus.CANCELLED if code is ToolCallErrorCode.CANCELLED else AgentRunStatus.FAILED
                await transition_locked_agent_run(
                    AgentEventRepository(session), run, AgentRunStatus.WAITING_APPROVAL, target,
                )

    async def decide(
        self, reference: ApprovalReference, decision: ApprovalDecision, *, context: PermissionContext,
    ) -> None:
        if type(decision) is not ApprovalDecision:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        operation = self._pending.resolve(reference, context)
        async with tool_call_transaction(self._session_factory) as session:
            run, call, approval = await self._load_locked(session, operation)
            self._pending.require_published(operation)
            self._check_binding(operation)
            self.check_deadline(operation)
            self._require_waiting(run, call)
            if approval.status != ApprovalStatus.PENDING.value or approval.decided_at is not None:
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            status = ApprovalStatus.APPROVED if decision is ApprovalDecision.APPROVE else ApprovalStatus.REJECTED
            await self._record_decision(session, reference, approval, status)
            if decision is ApprovalDecision.REJECT:
                await transition_locked_tool_call(
                    session, reference.ticket, call, ToolCallStatus.DENIED,
                    error_code=ToolCallErrorCode.APPROVAL_REJECTED,
                )
                await transition_locked_agent_run(
                    AgentEventRepository(session), run, AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.FAILED,
                )
        self._pending.notify(operation, decision)

    def _require_approved_waiter(self, operation: PendingOperation) -> None:
        if self._pending.require_published(operation).decision is not ApprovalDecision.APPROVE:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    async def claim(self, operation: PendingOperation) -> None:
        self._require_approved_waiter(operation)
        reference, ticket = operation.reference, operation.reference.ticket
        async with tool_call_transaction(self._session_factory) as session:
            run, call, approval = await self._load_locked(session, operation)
            self._require_approved_waiter(operation)
            self._check_binding(operation)
            self._require_waiting(run, call)
            if approval.status != ApprovalStatus.APPROVED.value or approval.decided_at is None:
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            decisions = await ApprovalRepository(session).event_payloads(
                ticket.run_id, reference.approval_id, APPROVAL_DECIDED,
            )
            if decisions != [self._decision_payload(reference, ApprovalStatus.APPROVED)]:
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            self.check_deadline(operation)
            await transition_locked_agent_run(
                AgentEventRepository(session), run, AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.RUNNING,
            )
            await transition_locked_tool_call(session, ticket, call, ToolCallStatus.RUNNING)

    async def finish(
        self, operation: PendingOperation, *, error: ToolCallErrorCode | None, duration_ms: int | None,
    ) -> None:
        self._pending.require_published(operation)
        async with tool_call_transaction(self._session_factory) as session:
            run, call, approval = await self._load_locked(session, operation)
            current = ToolCallStatus(call.status)
            if error is not None and current not in (ToolCallStatus.PROPOSED, ToolCallStatus.RUNNING):
                return  # A decision or another terminal path already resolved this call.
            if error is None and (current is not ToolCallStatus.RUNNING or approval.status != ApprovalStatus.APPROVED.value):
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            if error is not None and approval.status == ApprovalStatus.PENDING.value:
                await self._record_decision(
                    session, operation.reference, approval, ApprovalStatus.REJECTED, reason=error,
                )
            await transition_locked_tool_call(
                session, operation.reference.ticket, call,
                ToolCallStatus.SUCCEEDED if error is None else ToolCallStatus.FAILED,
                error_code=error, duration_ms=duration_ms if current is ToolCallStatus.RUNNING else None,
            )
            actual = AgentRunStatus(run.status)
            if error is not None and actual in (AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.RUNNING):
                target = AgentRunStatus.CANCELLED if error is ToolCallErrorCode.CANCELLED else AgentRunStatus.FAILED
                await transition_locked_agent_run(AgentEventRepository(session), run, actual, target)

    async def _load_locked(
        self, session: AsyncSession, operation: PendingOperation,
    ) -> tuple[AgentRunModel, ToolCallModel, ApprovalModel]:
        reference, ticket = operation.reference, operation.reference.ticket
        run = await lock_tool_call_run(session, ticket, require_running=False)
        call = await ToolCallRepository(session).get_for_update(ticket.run_id, ticket.tool_call_id)
        if call is None or call.tool_name != ticket.tool_name:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        repository = ApprovalRepository(session)
        approval = await repository.get_for_update(reference.approval_id, ticket.tool_call_id)
        requests = await repository.event_payloads(ticket.run_id, reference.approval_id, APPROVAL_REQUESTED)
        if approval is None or requests != [reference.requested_payload()]:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        return run, call, approval

    @staticmethod
    def _require_waiting(run: AgentRunModel, call: ToolCallModel) -> None:
        if run.status != AgentRunStatus.WAITING_APPROVAL.value or call.status != ToolCallStatus.PROPOSED.value:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    @staticmethod
    def _decision_payload(reference: ApprovalReference, target: ApprovalStatus) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "approval_id": str(reference.approval_id),
            "tool_call_id": str(reference.ticket.tool_call_id),
            "decision": target.value,
        }

    async def _record_decision(
        self, session: AsyncSession, reference: ApprovalReference, approval: ApprovalModel, target: ApprovalStatus,
        *, reason: ToolCallErrorCode | None = None,
    ) -> None:
        if approval.status != ApprovalStatus.PENDING.value or approval.decided_at is not None:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        approval.status = target.value
        approval.decided_at = datetime.now(UTC)
        payload = self._decision_payload(reference, target)
        if reason is not None:
            payload["error_code"] = reason.value
        await AgentEventRepository(session).append(
            reference.ticket.run_id, event_type=APPROVAL_DECIDED, payload=payload,
        )
