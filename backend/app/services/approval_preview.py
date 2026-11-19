"""Scoped approval ports and the read-only structural projection."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.schemas.approval import ApprovalPatchReview, ApprovalPreviewFile, LiveApprovalPreview
from app.services.approval_contracts import ApprovalDecision
from app.tools.repository.patch import PatchTextError, parse_patch


MAX_APPROVAL_PREVIEW_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ApprovalPreviewTarget:
    approval_id: UUID
    run_id: UUID
    workspace_id: UUID
    tool_call_id: UUID
    tool_name: str


@runtime_checkable
class ApprovalPreviewReader(Protocol):
    def read_approval_preview(self, target: ApprovalPreviewTarget) -> LiveApprovalPreview | None:
        ...


class ApprovalReviewIntegrityError(ValueError):
    pass


@runtime_checkable
class ApprovalReviewReader(Protocol):
    def read_approval_review(
        self, target: ApprovalPreviewTarget, request_events: Sequence[object],
    ) -> ApprovalPatchReview | None:
        ...


class ApprovalDecisionConflictError(RuntimeError):
    pass


@runtime_checkable
class ApprovalDecisionPort(Protocol):
    async def decide_approval(
        self, target: ApprovalPreviewTarget, request_events: Sequence[object],
        decision: ApprovalDecision, *, reviewed_patch_sha256: str | None,
    ) -> None:
        ...


def project_patch_preview(patch: str) -> LiveApprovalPreview | None:
    try:
        parsed = parse_patch(patch)
    except PatchTextError:
        return None
    preview = LiveApprovalPreview(files=tuple(
        ApprovalPreviewFile(
            path=file.path,
            added_lines=sum(line.prefix == "+" for hunk in file.hunks for line in hunk.lines),
            removed_lines=sum(line.prefix == "-" for hunk in file.hunks for line in hunk.lines),
            hunk_count=len(file.hunks),
        )
        for file in parsed.files
    ))
    if len(preview.model_dump_json().encode("utf-8")) > MAX_APPROVAL_PREVIEW_BYTES:
        return None
    return preview
