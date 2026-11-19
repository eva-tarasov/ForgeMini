from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agent_runs import _integer_query
from app.api.approvals import require_approval_workspace
from app.api.dependencies import get_session
from app.api.errors import ApiError
from app.schemas.agent_run import MAX_EVENT_SEQUENCE
from app.schemas.error import ErrorResponse
from app.schemas.evidence import MAX_EVIDENCE_PAGE, EvidenceAttemptDetail, EvidenceListResponse
from app.services.agent_runs import AgentRunNotFoundError
from app.services.evidence_reads import EvidenceAttemptNotFoundError, EvidenceReadService


router = APIRouter()
_READ_RESPONSES = {
    200: {"headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
    403: {"model": ErrorResponse, "description": "Read capability is missing or invalid"},
    404: {"model": ErrorResponse, "description": "Verification evidence or run not found"},
    422: {"model": ErrorResponse, "description": "Invalid identifiers or query"},
    500: {"model": ErrorResponse, "description": "Verification evidence read failed"},
    503: {"model": ErrorResponse, "description": "Local read capability is unavailable"},
}


@router.get("/agent-runs/{run_id}/evidence", response_model=EvidenceListResponse, responses=_READ_RESPONSES)
async def list_verification_evidence(
    run_id: UUID,
    response: Response,
    workspace_id: Annotated[UUID, Depends(require_approval_workspace)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE), BeforeValidator(_integer_query), Query()] = 0,
    limit: Annotated[int, Field(ge=1, le=MAX_EVIDENCE_PAGE), BeforeValidator(_integer_query), Query()] = MAX_EVIDENCE_PAGE,
) -> EvidenceListResponse:
    try:
        result = await EvidenceReadService(session).list_attempts(
            run_id, workspace_id, after_sequence=after_sequence, limit=limit,
        )
    except AgentRunNotFoundError:
        raise ApiError(404, "VERIFICATION_EVIDENCE_NOT_FOUND", "Verification evidence not found") from None
    except Exception:
        raise ApiError(500, "VERIFICATION_EVIDENCE_READ_FAILED", "Verification evidence read failed") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/agent-runs/{run_id}/evidence/{attempt_id}", response_model=EvidenceAttemptDetail, responses=_READ_RESPONSES,
)
async def get_verification_evidence(
    run_id: UUID,
    attempt_id: UUID,
    response: Response,
    workspace_id: Annotated[UUID, Depends(require_approval_workspace)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> EvidenceAttemptDetail:
    try:
        result = await EvidenceReadService(session).get_attempt(run_id, workspace_id, attempt_id)
    except (AgentRunNotFoundError, EvidenceAttemptNotFoundError):
        raise ApiError(404, "VERIFICATION_EVIDENCE_NOT_FOUND", "Verification evidence not found") from None
    except Exception:
        raise ApiError(500, "VERIFICATION_EVIDENCE_READ_FAILED", "Verification evidence read failed") from None
    response.headers["Cache-Control"] = "no-store"
    return result
