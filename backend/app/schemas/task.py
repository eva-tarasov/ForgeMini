from typing import Annotated
from uuid import UUID
from pydantic import BaseModel, Field, StringConstraints

AcceptanceCriterion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class TaskCreate(BaseModel):
    workspace_id: UUID
    goal: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
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
