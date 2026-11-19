from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.tools.repository.patch import MAX_FILE_LINES, MAX_PATCH_BYTES, MAX_PATCH_FILES, MAX_PATCH_HUNKS


class ApprovalListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending"] = "pending"


class PendingApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    run_id: UUID
    tool_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    status: Literal["pending"]
    requested_at: AwareDatetime


class PendingApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[PendingApprovalResponse]


class ApprovalPreviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    path: str
    added_lines: Annotated[int, Field(ge=0, le=MAX_FILE_LINES)]
    removed_lines: Annotated[int, Field(ge=0, le=MAX_FILE_LINES)]
    hunk_count: Annotated[int, Field(ge=1, le=MAX_PATCH_HUNKS)]


class LiveApprovalPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    state: Literal["live_at_read"] = "live_at_read"
    content_included: Literal[False] = False
    files: Annotated[tuple[ApprovalPreviewFile, ...], Field(min_length=1, max_length=MAX_PATCH_FILES)]


class UnavailableApprovalPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    state: Literal["unavailable"] = "unavailable"
    content_included: Literal[False] = False
    files: tuple[()] = ()


class ApprovalDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    run_id: UUID
    tool_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    status: Literal["pending", "approved", "rejected"]
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None
    preview: Annotated[LiveApprovalPreview | UnavailableApprovalPreview, Field(discriminator="state")]

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if (self.status == "pending") != (self.decided_at is None):
            raise ValueError("Invalid approval decision state")
        return self


class ApprovalPatchReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    approval_id: UUID
    run_id: UUID
    patch_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    patch: Annotated[str, Field(min_length=1, max_length=MAX_PATCH_BYTES, repr=False)]


class ApproveApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    decision: Literal["approve"]
    reviewed_patch_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", repr=False)]


class RejectApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    decision: Literal["reject"]


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    approval_id: UUID
    run_id: UUID
    decision: Literal["approved", "rejected"]
    recorded: Literal[True]
