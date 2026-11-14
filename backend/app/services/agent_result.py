from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class FixtureObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Literal["synthetic_fixture"] = "synthetic_fixture"
    tool_name: Literal["read_fixture"] = "read_fixture"
    component: Literal["task_service", "agent_run_service"]
    tool_call_id: str
    content: str

    @field_validator("tool_call_id", "content")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty_observation_field")
        return value


class AgentFinalResult(BaseModel):
    """Preliminary application result for later independent verification."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    summary: str
    status: Literal["ready_for_verification", "blocked"]
    relevant_components: tuple[str, ...]
    assumptions: tuple[str, ...]
    observations: tuple[FixtureObservation, ...]
    blocked_reason: str | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "ready_for_verification" and self.blocked_reason is not None:
            raise ValueError("unexpected_blocked_reason")
        if self.status == "blocked" and (self.blocked_reason is None or not self.blocked_reason.strip()):
            raise ValueError("missing_blocked_reason")
        if not self.summary.strip():
            raise ValueError("empty_summary")
        if any(not item.strip() for item in (*self.relevant_components, *self.assumptions)):
            raise ValueError("empty_analysis_item")
        return self
