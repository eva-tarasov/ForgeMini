"""Wire models for the public Health, Workspace, and Task endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

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


class ValidationIssue(BaseModel):
    field: str
    message: str


class ErrorData(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorData
