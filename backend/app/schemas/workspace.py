from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints


class WorkspaceCreate(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
