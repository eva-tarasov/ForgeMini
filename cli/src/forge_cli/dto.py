"""Wire models for public Forge endpoints."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
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


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str


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
    "invalid_runner_result", "execution_failed",
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


class RunMetadataEvent(_PublicEvent):
    kind: Literal[
        "tool_call_proposed", "tool_call_started", "tool_call_finished", "approval_requested",
        "approval_decided", "agent_usage_recorded", "other",
    ]
    details: EmptyEventDetails


AgentRunPublicEvent = Annotated[
    RunTransitionEvent | RunPreliminaryEvent | RunFailureEvent | RunVerificationEvent | RunMetadataEvent,
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
