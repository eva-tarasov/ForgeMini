from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    field: str
    message: str


class ErrorData(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorData
