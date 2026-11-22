"""Wire models for public Forge endpoints."""

from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    model_serializer,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

AcceptanceCriterion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class WorkspaceCreate(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    # ForgeMini run configuration; all optional so `forge workspace create --name X` is unchanged.
    source_path: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    test_command: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    image: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_config(self, handler):
        # Old request bodies were exactly {"name": ...}; keep that when nothing is configured.
        return {key: value for key, value in handler(self).items() if value is not None}


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    source_path: str | None = None
    test_command: str | None = None
    image: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_config(self, handler):
        # Unconfigured workspaces keep the exact pre-ForgeMini JSON shape ({"id","name"}).
        return {key: value for key, value in handler(self).items() if value is not None}


class TaskCreate(BaseModel):
    workspace_id: UUID
    goal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    acceptance_criteria: Annotated[
        list[AcceptanceCriterion],
        Field(min_length=1, max_length=10),
    ]


class TaskResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    goal: str
    acceptance_criteria: list[str]


AgentRunStatus = Literal[
    "queued", "running", "waiting_approval", "verifying", "succeeded", "failed", "cancelled"
]


def _parse_iso_datetime(value: object) -> datetime:
    # Pydantic also accepts epoch strings by default, whereas Forge emits ISO timestamps.
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value
    raise ValueError("Expected an ISO 8601 timestamp or datetime.")


WireDatetime = Annotated[AwareDatetime, BeforeValidator(_parse_iso_datetime)]


class AgentRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    attempt_number: Annotated[int, Field(ge=1, strict=True)]
    status: AgentRunStatus
    created_at: WireDatetime


class AgentRunStartResponse(BaseModel):
    run_id: UUID
    accepted: Literal[True]

    @field_validator("accepted", mode="before")
    @classmethod
    def require_true(cls, value: object) -> Literal[True]:
        # Literal[True] alone also matches numeric values equal to True.
        if value is not True:
            raise ValueError("Expected the JSON boolean true.")
        return True


MAX_EVENT_SEQUENCE = 2_147_483_647
MAX_EVENT_PAGE_SIZE = 100
PreliminaryStatus = Literal["ready_for_verification", "blocked"]
VerificationVerdict = Literal["pass", "fail", "inconclusive"]
AgentFailureCode = Literal[
    "execution_deadline_exceeded", "runner_timeout", "provider_timeout", "provider_connection",
    "provider_rate_limited", "provider_quota_exhausted", "provider_rate_limit_unknown",
    "provider_auth", "provider_request_rejected", "provider_unavailable", "provider_error",
    "graph_budget_exceeded", "graph_execution_failed", "structured_output_invalid",
    "invalid_runner_result", "sandbox_error", "workspace_not_configured", "execution_failed",
]


class _ReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunTransitionDetails(_ReadResponse):
    model_config = ConfigDict(serialize_by_alias=True)

    previous: AgentRunStatus = Field(alias="from")
    to: AgentRunStatus


class RunPreliminaryDetails(_ReadResponse):
    preliminary_status: PreliminaryStatus


class RunFailureDetails(_ReadResponse):
    code: AgentFailureCode


class RunVerificationDetails(_ReadResponse):
    verdict: VerificationVerdict


class EmptyEventDetails(_ReadResponse):
    pass


class RunStepDetails(_ReadResponse):
    step: Literal["model_response", "tool_result", "finish", "budget_exhausted", "error", "other"]
    summary: Annotated[str, Field(max_length=600)]
    # Structured twin of `summary`; absent on events written by older servers.
    turn: int | None = None
    tools: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=8)] | None = None
    tool: Annotated[str, Field(max_length=40)] | None = None
    target: Annotated[str, Field(max_length=200)] | None = None
    ok: bool | None = None
    exit_code: int | None = None
    outcome: Literal["claimed_done", "cannot_fix"] | None = None


class _PublicEvent(_ReadResponse):
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENT_SEQUENCE)]
    created_at: WireDatetime


class RunTransitionEvent(_PublicEvent):
    kind: Literal["agent_run_status_changed"]
    details: RunTransitionDetails


class RunPreliminaryEvent(_PublicEvent):
    kind: Literal["agent_result_recorded"]
    details: RunPreliminaryDetails


class RunFailureEvent(_PublicEvent):
    kind: Literal["agent_execution_failed"]
    details: RunFailureDetails


class RunVerificationEvent(_PublicEvent):
    kind: Literal["verification_recorded"]
    details: RunVerificationDetails


class RunStepEvent(_PublicEvent):
    kind: Literal["agent_step"]
    details: RunStepDetails


class RunMetadataEvent(_PublicEvent):
    kind: Literal[
        "tool_call_proposed", "tool_call_started", "tool_call_finished", "approval_requested",
        "approval_decided", "agent_usage_recorded", "other",
    ]
    details: EmptyEventDetails


AgentRunPublicEvent = Annotated[
    RunTransitionEvent | RunPreliminaryEvent | RunFailureEvent | RunVerificationEvent | RunStepEvent
    | RunMetadataEvent,
    Field(discriminator="kind"),
]


class AgentRunEventsResponse(_ReadResponse):
    items: Annotated[list[AgentRunPublicEvent], Field(max_length=MAX_EVENT_PAGE_SIZE)]
    next_after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE)]
    has_more: bool


class AgentRunUsageResponse(_ReadResponse):
    known_total_tokens: Annotated[int, Field(ge=0)]
    usage_complete: bool
    known_cost_usd: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")] | None
    cost_complete: bool

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.cost_complete and (not self.usage_complete or self.known_cost_usd is None):
            raise ValueError("Complete cost requires complete usage and a known cost.")
        return self


class AgentRunResultResponse(_ReadResponse):
    run_id: UUID
    run_status: AgentRunStatus
    preliminary_status: PreliminaryStatus | None
    execution_failure_code: AgentFailureCode | None
    verification_verdict: VerificationVerdict | None
    usage: AgentRunUsageResponse | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        status = self.run_status
        preliminary = self.preliminary_status
        verdict = self.verification_verdict
        requires_preliminary = status in ("verifying", "succeeded") or verdict is not None
        if (
            (requires_preliminary and preliminary != "ready_for_verification")
            or (preliminary == "blocked" and status != "failed")
            or (preliminary is not None and status in ("queued", "running", "waiting_approval"))
            or (self.execution_failure_code is not None and (status != "failed" or preliminary is not None))
            or ((verdict == "pass") != (status == "succeeded"))
            or (verdict == "fail" and status != "failed")
        ):
            raise ValueError("Inconsistent AgentRun result metadata.")
        return self


class ValidationIssue(BaseModel):
    field: str
    message: str


class ErrorData(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorData


class _CapabilityResponse(_ReadResponse):
    model_config = ConfigDict(hide_input_in_errors=True)


class PendingApprovalResponse(_CapabilityResponse):
    id: UUID
    run_id: UUID
    tool_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    status: Literal["pending"]
    requested_at: WireDatetime


class PendingApprovalListResponse(_CapabilityResponse):
    items: Annotated[list[PendingApprovalResponse], Field(max_length=32)]


class ApprovalPreviewFile(_CapabilityResponse):
    path: str
    added_lines: Annotated[int, Field(ge=0, le=1000)]
    removed_lines: Annotated[int, Field(ge=0, le=1000)]
    hunk_count: Annotated[int, Field(ge=1, le=32)]


class _ApprovalPreview(_CapabilityResponse):
    content_included: bool

    @field_validator("content_included")
    @classmethod
    def exclude_content(cls, value: bool) -> bool:
        if value:
            raise ValueError("Preview must exclude content.")
        return value


class LiveApprovalPreview(_ApprovalPreview):
    state: Literal["live_at_read"]
    files: Annotated[tuple[ApprovalPreviewFile, ...], Field(min_length=1, max_length=4)]


class UnavailableApprovalPreview(_ApprovalPreview):
    state: Literal["unavailable"]
    files: tuple[()]


class ApprovalDetailResponse(_CapabilityResponse):
    id: UUID
    run_id: UUID
    tool_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    status: Literal["pending", "approved", "rejected"]
    requested_at: WireDatetime
    decided_at: WireDatetime | None
    preview: Annotated[LiveApprovalPreview | UnavailableApprovalPreview, Field(discriminator="state")]

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if (self.status == "pending") != (self.decided_at is None):
            raise ValueError("Invalid approval decision state.")
        return self


class ApprovalPatchReview(_CapabilityResponse):
    approval_id: UUID
    run_id: UUID
    patch_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    patch: Annotated[str, Field(min_length=1, max_length=65536, repr=False)]

    @model_validator(mode="after")
    def check_patch(self) -> Self:
        try:
            encoded = self.patch.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Invalid patch encoding.") from None
        if len(encoded) > 65536 or sha256(b"forge:apply_patch:v1\0" + encoded).hexdigest() != self.patch_sha256:
            raise ValueError("Invalid patch integrity.")
        return self


class EvidenceAttemptSummary(_CapabilityResponse):
    attempt_id: UUID
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENT_SEQUENCE)]
    verdict: VerificationVerdict
    file_verdict: VerificationVerdict
    audit_complete: bool
    isolated_fixture: bool
    check_count: Annotated[int, Field(ge=1, le=32)]


class EvidenceListResponse(_CapabilityResponse):
    run_id: UUID
    items: Annotated[list[EvidenceAttemptSummary], Field(max_length=20)]
    next_after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE)]
    has_more: bool


class EvidenceCheckResponse(_CapabilityResponse):
    check_id: UUID
    criterion_index: Annotated[int, Field(ge=0, le=9)]
    path: Annotated[str, Field(repr=False)]
    outcome: Literal["match", "mismatch", "read_error", "unknown_effect", "ambiguous", "missing"]


class EvidenceAttemptDetail(EvidenceAttemptSummary):
    checks: Annotated[list[EvidenceCheckResponse], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def check_length(self) -> Self:
        if self.check_count != len(self.checks):
            raise ValueError("Evidence check count mismatch.")
        return self


class ApproveApprovalRequest(_CapabilityResponse):
    decision: Literal["approve"] = "approve"
    reviewed_patch_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", repr=False)]


class RejectApprovalRequest(_CapabilityResponse):
    decision: Literal["reject"] = "reject"


class ApprovalDecisionResponse(_CapabilityResponse):
    approval_id: UUID
    run_id: UUID
    decision: Literal["approved", "rejected"]
    recorded: Literal[True]

    @field_validator("recorded", mode="before")
    @classmethod
    def require_recorded(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("Expected the JSON boolean true.")
        return True


class CommandRunView(_ReadResponse):
    exit_code: int
    timed_out: bool
    output_tail: str


class VerificationView(_ReadResponse):
    verdict: VerificationVerdict
    reason: str
    test_command: str
    image: str
    changed_paths: list[str]
    protected_violations: list[str]
    patched: CommandRunView | None
    baseline: CommandRunView | None
    duration_ms: int


class UsageView(_ReadResponse):
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


class RunReport(_ReadResponse):
    """GET /agent-runs/{id}/report: the one-call answer to "what happened and why?"."""

    run_id: UUID
    task_id: UUID
    run_status: AgentRunStatus
    task_status: str
    goal: str
    agent_summary: str | None
    blocked_reason: str | None
    failure_code: str | None
    verification: VerificationView | None
    has_patch: bool
    usage: UsageView | None = None
