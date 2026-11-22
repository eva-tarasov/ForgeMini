from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.execution import AgentRunStatus
from app.services.agent_failure import AgentFailureCode
from app.services.verification_contract import VerificationVerdict


class AgentRunCreate(BaseModel):
    """Optional empty JSON object. All run metadata is assigned by Forge."""

    model_config = ConfigDict(extra="forbid")


class AgentRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    attempt_number: Annotated[int, Field(ge=1)]
    status: AgentRunStatus
    created_at: AwareDatetime


class AgentRunCommand(BaseModel):
    """Run commands accept an optional empty object."""

    model_config = ConfigDict(extra="forbid")


class AgentRunStartResponse(BaseModel):
    run_id: UUID
    accepted: Literal[True]


PreliminaryStatus = Literal["ready_for_verification", "blocked"]
MAX_EVENT_SEQUENCE = 2_147_483_647


class _ReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunTransitionDetails(_ReadResponse):
    previous: AgentRunStatus = Field(serialization_alias="from")
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


class _PublicEvent(_ReadResponse):
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENT_SEQUENCE)]
    created_at: AwareDatetime


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
    items: Annotated[list[AgentRunPublicEvent], Field(max_length=100)]
    next_after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE)]
    has_more: bool


class AgentRunUsageResponse(_ReadResponse):
    known_total_tokens: Annotated[int, Field(ge=0)]
    usage_complete: bool
    known_cost_usd: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")] | None
    cost_complete: bool


class AgentRunResultResponse(_ReadResponse):
    run_id: UUID
    run_status: AgentRunStatus
    preliminary_status: PreliminaryStatus | None
    execution_failure_code: AgentFailureCode | None
    verification_verdict: VerificationVerdict | None
    usage: AgentRunUsageResponse | None
