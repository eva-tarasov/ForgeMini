from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.capabilities import Capability


class ToolRiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolSpec(BaseModel):
    """Internal tool metadata declaring the capability required for execution."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=500)
    input_schema: type[ToolInput]
    output_schema: type[ToolOutput]
    capability: Capability
    risk_level: ToolRiskLevel
    timeout_ms: int = Field(ge=1, le=30_000)
    side_effect: bool
    requires_approval: bool
    # Default policy for operational logs.
    audit_policy: Literal["metadata_only"] = "metadata_only"

    @model_validator(mode="after")
    def validate_security_metadata(self) -> Self:
        if self.capability is Capability.REPOSITORY_WRITE:
            if not self.side_effect:
                raise ValueError("repository.write requires side_effect=True")
            if not self.requires_approval:
                raise ValueError("repository.write requires requires_approval=True")
            if self.risk_level is not ToolRiskLevel.HIGH:
                raise ValueError("repository.write requires high risk")

        if self.capability is Capability.REPOSITORY_READ:
            if self.side_effect:
                raise ValueError("repository.read requires side_effect=False")
            if self.risk_level is not ToolRiskLevel.LOW:
                raise ValueError("repository.read requires low risk")

        return self
