from typing import Literal

from pydantic import BaseModel, ConfigDict


class FixtureObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Literal["synthetic_fixture"] = "synthetic_fixture"
    tool_name: Literal["read_fixture"] = "read_fixture"
    component: Literal["task_service", "agent_run_service"]
    tool_call_id: str
    content: str


class AgentFinalResult(BaseModel):
    """Preliminary runtime result for later independent verification."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    summary: str
    status: Literal["ready_for_verification", "blocked"]
    relevant_components: tuple[str, ...]
    assumptions: tuple[str, ...]
    observations: tuple[FixtureObservation, ...]
    blocked_reason: str | None
