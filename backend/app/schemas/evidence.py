from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agent_run import MAX_EVENT_SEQUENCE
from app.services.verification_contract import MAX_CHECKS, MAX_CRITERIA, VerificationVerdict


MAX_EVIDENCE_PAGE = 20
EvidenceOutcome = Literal["match", "mismatch", "read_error", "unknown_effect", "ambiguous", "missing"]


class _EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EvidenceAttemptSummary(_EvidenceResponse):
    attempt_id: UUID
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENT_SEQUENCE)]
    verdict: VerificationVerdict
    file_verdict: VerificationVerdict
    audit_complete: bool
    isolated_fixture: bool
    check_count: Annotated[int, Field(ge=1, le=MAX_CHECKS)]


class EvidenceCheckResponse(_EvidenceResponse):
    check_id: UUID
    criterion_index: Annotated[int, Field(ge=0, lt=MAX_CRITERIA)]
    path: Annotated[str, Field(repr=False)]
    outcome: EvidenceOutcome


class EvidenceAttemptDetail(EvidenceAttemptSummary):
    checks: Annotated[list[EvidenceCheckResponse], Field(min_length=1, max_length=MAX_CHECKS)]


class EvidenceListResponse(_EvidenceResponse):
    run_id: UUID
    items: Annotated[list[EvidenceAttemptSummary], Field(max_length=MAX_EVIDENCE_PAGE)]
    next_after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE)]
    has_more: bool
