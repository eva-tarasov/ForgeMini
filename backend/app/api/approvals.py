from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agent_runs import get_run_coordinator
from app.api.dependencies import get_session
from app.api.errors import ApiError
from app.schemas.approval import (
    ApprovalDecisionResponse, ApprovalDetailResponse, ApprovalListQuery, ApprovalPatchReview,
    ApproveApprovalRequest, PendingApprovalListResponse, RejectApprovalRequest,
)
from app.schemas.error import ErrorResponse
from app.services.agent_runs import AgentRunNotFoundError
from app.services.approval_contracts import ApprovalDecision
from app.services.approval_decisions import ApprovalDecisionService, ApprovalDecisionUnavailableError
from app.services.approval_preview import ApprovalDecisionConflictError, ApprovalReviewIntegrityError
from app.services.approval_reads import (
    ApprovalNotFoundError, ApprovalNotReviewableError, ApprovalReadService, ApprovalReviewUnavailableError,
)
from app.services.in_process_run_coordinator import InProcessRunCoordinator
from app.services.local_approval_access import LocalApprovalAccessBinding


router = APIRouter()
_bearer = HTTPBearer(scheme_name="LocalApprovalCapability", auto_error=False)
_review_bearer = HTTPBearer(scheme_name="LocalApprovalReviewCapability", auto_error=False)


async def require_approval_workspace(
    request: Request,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UUID:
    binding: LocalApprovalAccessBinding | None = request.app.state.approval_access_binding
    if binding is None:
        raise ApiError(503, "APPROVAL_API_UNAVAILABLE", "Local approval API is unavailable")
    if (
        credential is None
        or len(request.headers.getlist("authorization")) != 1
        or not binding.accepts(credential.credentials)
    ):
        raise ApiError(403, "APPROVAL_ACCESS_DENIED", "Approval access denied")
    return binding.workspace_id


def _review_unavailable() -> ApiError:
    return ApiError(503, "APPROVAL_REVIEW_UNAVAILABLE", "Local approval review is unavailable")


def _review_workspace(request: Request, credential: HTTPAuthorizationCredentials | None) -> UUID | None:
    binding: LocalApprovalAccessBinding | None = request.app.state.approval_access_binding
    if binding is None or binding.review_token is None:
        return None
    if (
        credential is None
        or len(request.headers.getlist("authorization")) != 1
        or not binding.accepts_review(credential.credentials)
    ):
        raise ApiError(403, "APPROVAL_ACCESS_DENIED", "Approval access denied")
    return binding.workspace_id


async def require_approval_review_workspace(
    request: Request,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_review_bearer)],
) -> UUID:
    workspace_id = _review_workspace(request, credential)
    if workspace_id is None:
        raise _review_unavailable()
    return workspace_id


def _decision_unavailable() -> ApiError:
    return ApiError(503, "APPROVAL_DECISION_UNAVAILABLE", "Local approval decision is unavailable")


async def require_approval_decision_workspace(
    request: Request,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_review_bearer)],
) -> UUID:
    workspace_id = _review_workspace(request, credential)
    if workspace_id is None:
        raise _decision_unavailable()
    return workspace_id


@router.get(
    "/agent-runs/{run_id}/approvals",
    response_model=PendingApprovalListResponse,
    responses={
        403: {"model": ErrorResponse, "description": "Approval capability missing or invalid"},
        404: {"model": ErrorResponse, "description": "AgentRun not found in the allowed workspace"},
        422: {"model": ErrorResponse, "description": "Invalid run ID or query"},
        500: {"model": ErrorResponse, "description": "Approval data is unavailable"},
        503: {"model": ErrorResponse, "description": "Local approval access is unavailable"},
    },
)
async def list_pending_approvals(
    run_id: UUID,
    workspace_id: Annotated[UUID, Depends(require_approval_workspace)],
    query: Annotated[ApprovalListQuery, Query()],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> PendingApprovalListResponse:
    try:
        return await ApprovalReadService(session).list_pending(run_id, workspace_id)
    except AgentRunNotFoundError:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from None
    except (SQLAlchemyError, ValidationError):
        raise ApiError(500, "APPROVAL_READ_FAILED", "Approval data is unavailable") from None


@router.get(
    "/agent-runs/{run_id}/approvals/{approval_id}",
    response_model=ApprovalDetailResponse,
    responses={
        403: {"model": ErrorResponse, "description": "Approval capability missing or invalid"},
        404: {"model": ErrorResponse, "description": "Approval not found"},
        422: {"model": ErrorResponse, "description": "Invalid run or approval ID"},
        500: {"model": ErrorResponse, "description": "Approval data is unavailable"},
        503: {"model": ErrorResponse, "description": "Local approval access is unavailable"},
    },
)
async def get_approval_detail(
    run_id: UUID,
    approval_id: UUID,
    workspace_id: Annotated[UUID, Depends(require_approval_workspace)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    coordinator: Annotated[InProcessRunCoordinator | None, Depends(get_run_coordinator)],
) -> ApprovalDetailResponse:
    try:
        return await ApprovalReadService(session).read_detail(approval_id, run_id, workspace_id, coordinator)
    except ApprovalNotFoundError:
        raise ApiError(404, "APPROVAL_NOT_FOUND", "Approval not found") from None
    except (SQLAlchemyError, ValidationError):
        raise ApiError(500, "APPROVAL_READ_FAILED", "Approval data is unavailable") from None


@router.get(
    "/agent-runs/{run_id}/approvals/{approval_id}/review",
    response_model=ApprovalPatchReview,
    responses={
        200: {"headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
        403: {"model": ErrorResponse, "description": "Review capability missing or invalid"},
        404: {"model": ErrorResponse, "description": "Approval not found"},
        409: {"model": ErrorResponse, "description": "Approval is no longer reviewable"},
        422: {"model": ErrorResponse, "description": "Invalid run or approval ID"},
        500: {"model": ErrorResponse, "description": "Approval review failed"},
        503: {"model": ErrorResponse, "description": "Approval review capability or runtime is unavailable"},
    },
)
async def get_approval_review(
    run_id: UUID,
    approval_id: UUID,
    response: Response,
    workspace_id: Annotated[UUID, Depends(require_approval_review_workspace)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    coordinator: Annotated[InProcessRunCoordinator | None, Depends(get_run_coordinator)],
) -> ApprovalPatchReview:
    try:
        review = await ApprovalReadService(session).read_review(approval_id, run_id, workspace_id, coordinator)
    except ApprovalNotFoundError:
        raise ApiError(404, "APPROVAL_NOT_FOUND", "Approval not found") from None
    except ApprovalNotReviewableError:
        raise ApiError(409, "APPROVAL_NOT_REVIEWABLE", "Approval is no longer reviewable") from None
    except ApprovalReviewUnavailableError:
        raise _review_unavailable() from None
    except (SQLAlchemyError, ValidationError, ApprovalReviewIntegrityError):
        raise ApiError(500, "APPROVAL_REVIEW_FAILED", "Approval review failed") from None
    response.headers["Cache-Control"] = "no-store"
    return review


@router.post(
    "/agent-runs/{run_id}/approvals/{approval_id}/decision",
    response_model=ApprovalDecisionResponse,
    responses={
        200: {"headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
        403: {"model": ErrorResponse, "description": "Decision capability missing or invalid"},
        404: {"model": ErrorResponse, "description": "Approval not found"},
        409: {"model": ErrorResponse, "description": "Approval decision conflict"},
        422: {"model": ErrorResponse, "description": "Invalid identifiers or decision body"},
        500: {"model": ErrorResponse, "description": "Approval decision failed"},
        503: {"model": ErrorResponse, "description": "Approval decision capability or runtime is unavailable"},
    },
)
async def post_approval_decision(
    run_id: UUID,
    approval_id: UUID,
    response: Response,
    workspace_id: Annotated[UUID, Depends(require_approval_decision_workspace)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    coordinator: Annotated[InProcessRunCoordinator | None, Depends(get_run_coordinator)],
    body: Annotated[ApproveApprovalRequest | RejectApprovalRequest, Body(discriminator="decision")],
) -> ApprovalDecisionResponse:
    try:
        result = await ApprovalDecisionService(session).decide(
            approval_id, run_id, workspace_id, ApprovalDecision(body.decision),
            reviewed_patch_sha256=body.reviewed_patch_sha256 if isinstance(body, ApproveApprovalRequest) else None,
            port=coordinator,
        )
    except ApprovalNotFoundError:
        raise ApiError(404, "APPROVAL_NOT_FOUND", "Approval not found") from None
    except (ApprovalNotReviewableError, ApprovalDecisionConflictError):
        raise ApiError(409, "APPROVAL_DECISION_CONFLICT", "Approval decision conflicts with current state") from None
    except ApprovalDecisionUnavailableError:
        raise _decision_unavailable() from None
    except Exception:
        raise ApiError(500, "APPROVAL_DECISION_FAILED", "Approval decision failed") from None
    response.headers["Cache-Control"] = "no-store"
    return result
