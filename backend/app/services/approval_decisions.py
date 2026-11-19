"""Bind a decision to a scoped approval and dispatch it to the live runtime."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.approval import ApprovalDecisionResponse
from app.services.approval_contracts import ApprovalDecision
from app.services.approval_preview import ApprovalDecisionPort
from app.services.approval_reads import ApprovalReadService


class ApprovalDecisionUnavailableError(RuntimeError):
    pass


class ApprovalDecisionService:
    def __init__(self, session: AsyncSession) -> None:
        self._approvals = ApprovalReadService(session)

    async def decide(
        self, approval_id: UUID, run_id: UUID, workspace_id: UUID, decision: ApprovalDecision,
        *, reviewed_patch_sha256: str | None, port: ApprovalDecisionPort | None,
    ) -> ApprovalDecisionResponse:
        target, request_events = await self._approvals.load_pending_target(approval_id, run_id, workspace_id)
        if port is None:
            raise ApprovalDecisionUnavailableError()
        await port.decide_approval(target, request_events, decision, reviewed_patch_sha256=reviewed_patch_sha256)
        return ApprovalDecisionResponse(
            approval_id=target.approval_id,
            run_id=target.run_id,
            decision="approved" if decision is ApprovalDecision.APPROVE else "rejected",
            recorded=True,
        )
