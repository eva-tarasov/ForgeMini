import asyncio
from dataclasses import dataclass
from uuid import UUID

from app.permissions.context import PermissionContext
from app.services.approval_contracts import ApprovalDecision, ApprovalReference, PendingOperation
from app.tools.errors import ToolErrorCode, ToolExecutionError


@dataclass(slots=True)
class _Wait:
    operation: PendingOperation
    ready: asyncio.Event
    published: bool = False
    decision: ApprovalDecision | None = None


class PendingApprovals:
    """Loop-owned waiter slots. Reserved operations are invisible until publish."""

    def __init__(self, capacity: int = 32) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError("invalid_pending_approval_capacity")
        self._capacity = capacity
        self._loop: asyncio.AbstractEventLoop | None = None
        self._entries: dict[UUID, _Wait] = {}

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._loop is not loop:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    def reserve(self, operation: PendingOperation) -> None:
        self._check_loop()
        if type(operation) is not PendingOperation:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        operation.check_identity()
        ref = operation.reference
        if (
            len(self._entries) >= self._capacity or ref.approval_id in self._entries
            or any(slot.operation.reference.ticket.run_id == ref.ticket.run_id for slot in self._entries.values())
        ):
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        self._entries[ref.approval_id] = _Wait(operation, asyncio.Event())

    def _slot(self, operation: PendingOperation) -> _Wait:
        self._check_loop()
        if type(operation) is not PendingOperation:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        slot = self._entries.get(operation.reference.approval_id)
        if slot is None or slot.operation is not operation:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        return slot

    def require_reserved(self, operation: PendingOperation) -> None:
        if self._slot(operation).published:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    def publish(self, operation: PendingOperation) -> None:
        self._slot(operation).published = True

    def _published_slot_for(self, context: PermissionContext) -> _Wait | None:
        self._check_loop()
        for slot in self._entries.values():
            if slot.published and slot.operation.context is context:
                return slot
        return None

    def request_for(self, context: PermissionContext) -> ApprovalReference | None:
        slot = self._published_slot_for(context)
        return slot.operation.reference if slot is not None else None

    def _undecided_operation_for(self, context: PermissionContext) -> PendingOperation | None:
        slot = self._published_slot_for(context)
        return slot.operation if slot is not None and slot.decision is None else None

    def resolve(self, reference: ApprovalReference, context: PermissionContext) -> PendingOperation:
        self._check_loop()
        if type(reference) is ApprovalReference:
            slot = self._entries.get(reference.approval_id)
            if slot is not None and slot.published and slot.operation.context is context:
                if slot.operation.reference == reference:
                    return slot.operation
        raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    def require_published(self, operation: PendingOperation) -> _Wait:
        slot = self._slot(operation)
        if not slot.published:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        return slot

    async def wait(self, operation: PendingOperation) -> ApprovalDecision:
        slot = self.require_published(operation)
        async with asyncio.timeout_at(operation.deadline):
            await slot.ready.wait()
        assert slot.decision is not None
        return slot.decision

    def notify(self, operation: PendingOperation, decision: ApprovalDecision) -> None:
        self._check_loop()
        slot = self._entries.get(operation.reference.approval_id)
        # A timed out or cancelled waiter may already have removed its slot.
        if slot is not None and slot.operation is operation and slot.published and slot.decision is None:
            slot.decision = decision
            slot.ready.set()

    def discard(self, operation: PendingOperation) -> None:
        self._check_loop()
        slot = self._entries.get(operation.reference.approval_id)
        if slot is not None and slot.operation is operation:
            del self._entries[operation.reference.approval_id]
