import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
from time import perf_counter_ns

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.permissions.context import PermissionContext
from app.services.approval_contracts import ApprovalDecision, ApprovalReference, PendingOperation, validate_patch_proposal
from app.services.approval_gate import ApprovalGate
from app.services.approval_outcome import ApprovalOutcome, ApprovalOutcomePort
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import _finish_interrupted_proposal
from app.tools.contracts import ToolOutput
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.repository.schemas import ApplyPatchInput
from app.tools.repository.writer import PatchApplyError


@dataclass(frozen=True, slots=True)
class _ApprovalPreview:
    reference: ApprovalReference
    patch: str = field(repr=False)


class ApprovedToolExecutor:
    """A trusted in-process fixture approval coordinator, with no recovery path."""

    def __init__(
        self, executor: ToolExecutor, session_factory: async_sessionmaker[AsyncSession], *,
        wait_seconds: float = 60, capacity: int = 32, outcome: ApprovalOutcomePort | None = None,
    ) -> None:
        if type(wait_seconds) not in (int, float) or not 0 < wait_seconds <= 300:
            raise ValueError("invalid_approval_wait_seconds")
        self._executor = executor
        self._wait_seconds = wait_seconds
        self._outcome = outcome
        self._pending = PendingApprovals(capacity)
        self._gate = ApprovalGate(executor, session_factory, self._pending)

    def pending_request(self, context: PermissionContext) -> ApprovalReference | None:
        return self._pending.request_for(context)

    def _pending_preview(self, context: PermissionContext) -> _ApprovalPreview | None:
        operation = self._pending._undecided_operation_for(context)
        if operation is None:
            return None
        return _ApprovalPreview(operation.reference, operation.patch)

    async def decide(
        self, reference: ApprovalReference, decision: ApprovalDecision, *, context: PermissionContext,
    ) -> None:
        await self._gate.decide(reference, decision, context=context)

    async def execute(
        self, name: str, arguments: dict[str, object], *, version: int | None = None,
        context: PermissionContext | None = None,
    ) -> ToolOutput:
        task = asyncio.current_task()

        def check_cancellation() -> None:
            if task is not None and task.cancelling():
                raise asyncio.CancelledError

        check_cancellation()
        tool, typed, writer = validate_patch_proposal(
            self._executor, name, arguments, version=version, context=context,
        )
        try:
            sources = writer.preview(typed.patch)
        except PatchApplyError:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_SOURCE_CHANGED) from None
        assert context is not None
        operation = PendingOperation.create(
            context, tool, writer, typed, sources, asyncio.get_running_loop().time() + self._wait_seconds,
        )
        self._pending.reserve(operation)
        started_at: int | None = None
        try:
            if self._outcome is not None and not self._outcome.bind(operation.reference):
                raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
            try:
                await self._gate.request(operation)
            except (asyncio.CancelledError, ToolCallAuditError) as error:
                if isinstance(error, asyncio.CancelledError) or error.code is ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED:
                    code = ToolCallErrorCode.CANCELLED if isinstance(error, asyncio.CancelledError) else error.code
                    self._publish_outcome(operation.reference, code, error, interrupted_request=True)
                    await _finish_interrupted_proposal(self._gate._reconcile_unpublished_request(operation, code), error)
                raise
            try:
                check_cancellation()
                try:
                    decision = await self._pending.wait(operation)
                except TimeoutError:
                    raise ToolExecutionError(ToolErrorCode.APPROVAL_TIMEOUT) from None
                check_cancellation()
                if decision is ApprovalDecision.REJECT:
                    raise ToolExecutionError(ToolErrorCode.APPROVAL_REJECTED)
                self._gate.check_deadline(operation)
                try:
                    current_sources = operation.writer.preview(operation.patch)
                except PatchApplyError:
                    raise ToolExecutionError(ToolErrorCode.APPROVAL_SOURCE_CHANGED) from None
                if current_sources != operation.sources:
                    raise ToolExecutionError(ToolErrorCode.APPROVAL_SOURCE_CHANGED)
                check_cancellation()
                await self._gate.claim(operation)
                started_at = perf_counter_ns()
                check_cancellation()
                self._gate.check_deadline(operation)
                self._gate._check_binding(operation)
                check_cancellation()
                # Only the exact registered handler receives the immutable source evidence.
                bound = replace(operation.tool, handler=partial(
                    operation.tool.handler, expected_sources=operation.sources,
                ))
                output = await self._executor._invoke_handler(bound, ApplyPatchInput(patch=operation.patch))
                check_cancellation()
            except (asyncio.CancelledError, ToolCallAuditError, ToolExecutionError) as error:
                await self._finish_error(operation, error, started_at)
                raise
            await self._gate.finish(operation, error=None, duration_ms=_duration(started_at))
            check_cancellation()
            return output
        finally:
            self._pending.discard(operation)

    async def _finish_error(
        self, operation: PendingOperation,
        error: asyncio.CancelledError | ToolCallAuditError | ToolExecutionError,
        started_at: int | None,
    ) -> None:
        if isinstance(error, asyncio.CancelledError):
            code = ToolCallErrorCode.CANCELLED
        elif error.code in (ToolErrorCode.PERMISSION_DENIED, ToolErrorCode.EXECUTION_BLOCKED):
            code = ToolCallErrorCode.APPROVAL_INVALID
        else:
            code = ToolCallErrorCode(error.code.value)
        try:
            if code is not ToolCallErrorCode.APPROVAL_REJECTED:
                await self._gate.finish(operation, error=code, duration_ms=_duration(started_at))
        except Exception:
            error.add_note("tool_call_audit_finalization_failed")
        else:
            self._publish_outcome(operation.reference, code, error)

    def _publish_outcome(
        self, reference: ApprovalReference, code: ToolCallErrorCode, error: BaseException, *,
        interrupted_request: bool = False,
    ) -> None:
        if self._outcome is not None:
            try:
                self._outcome.publish(ApprovalOutcome(reference, code, interrupted_request))
            except (asyncio.CancelledError, Exception):
                # Синхронная запись метаданных сохраняет исходную ошибку и путь cleanup.
                error.add_note("approval_outcome_publication_failed")


def _duration(started_at: int | None) -> int | None:
    return None if started_at is None else (perf_counter_ns() - started_at) // 1_000_000
