from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import AgentRunStatus, ApprovalStatus, ToolCallStatus
from app.repositories.approvals import ApprovalRepository
from app.schemas.approval import (
    ApprovalDetailResponse, ApprovalPatchReview, PendingApprovalListResponse, PendingApprovalResponse,
    UnavailableApprovalPreview,
)
from app.services.agent_runs import AgentRunNotFoundError
from app.services.approval_gate import APPROVAL_REQUESTED
from app.services.approval_preview import ApprovalPreviewReader, ApprovalPreviewTarget, ApprovalReviewReader


MAX_PENDING_APPROVALS = 32


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalNotReviewableError(LookupError):
    pass


class ApprovalReviewUnavailableError(RuntimeError):
    pass


def _pending_target(row: RowMapping) -> ApprovalPreviewTarget | None:
    if (
        row["status"] == ApprovalStatus.PENDING.value and row["decided_at"] is None
        and row["run_status"] == AgentRunStatus.WAITING_APPROVAL.value
        and row["tool_call_status"] == ToolCallStatus.PROPOSED.value
    ):
        return ApprovalPreviewTarget(row["id"], row["run_id"], row["workspace_id"], row["tool_call_id"], row["tool_name"])
    return None


class ApprovalReadService:
    def __init__(self, session: AsyncSession) -> None:
        self._approvals = ApprovalRepository(session)

    async def list_pending(self, run_id: UUID, workspace_id: UUID) -> PendingApprovalListResponse:
        rows = await self._approvals.read_pending_for_run(run_id, workspace_id, limit=MAX_PENDING_APPROVALS)
        if not rows:
            raise AgentRunNotFoundError(run_id)
        return PendingApprovalListResponse(items=[
            PendingApprovalResponse.model_validate(dict(row)) for row in rows if row["id"] is not None
        ])

    async def read_detail(
        self, approval_id: UUID, run_id: UUID, workspace_id: UUID, reader: ApprovalPreviewReader | None,
    ) -> ApprovalDetailResponse:
        row = await self._approvals.read_for_run(approval_id, run_id, workspace_id)
        if row is None:
            raise ApprovalNotFoundError()

        target = _pending_target(row)
        preview = reader.read_approval_preview(target) if reader is not None and target is not None else None
        return ApprovalDetailResponse(
            id=row["id"], run_id=row["run_id"], tool_name=row["tool_name"], status=row["status"],
            requested_at=row["requested_at"], decided_at=row["decided_at"],
            preview=preview if preview is not None else UnavailableApprovalPreview(),
        )

    async def read_review(
        self, approval_id: UUID, run_id: UUID, workspace_id: UUID, reader: ApprovalReviewReader | None,
    ) -> ApprovalPatchReview:
        target, request_events = await self.load_pending_target(approval_id, run_id, workspace_id)
        if reader is None:
            raise ApprovalReviewUnavailableError()
        review = reader.read_approval_review(target, request_events)
        if review is None:
            raise ApprovalNotReviewableError()
        return review

    async def load_pending_target(
        self, approval_id: UUID, run_id: UUID, workspace_id: UUID,
    ) -> tuple[ApprovalPreviewTarget, Sequence[object]]:
        """Read the scoped operation identity and request evidence in one SQL snapshot."""
        row = await self._approvals.read_for_review(
            approval_id, run_id, workspace_id, request_event_type=APPROVAL_REQUESTED,
        )
        if row is None:
            raise ApprovalNotFoundError()
        target = _pending_target(row)
        if target is None:
            raise ApprovalNotReviewableError()
        return target, row.get("request_events") or ()
