import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BeforeValidator, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.api.errors import ApiError
from app.domain.transitions import InvalidAgentRunTransition
from app.schemas.agent_run import (
    MAX_EVENT_SEQUENCE, AgentRunCommand, AgentRunCreate, AgentRunEventsResponse,
    AgentRunResponse, AgentRunResultResponse, AgentRunStartResponse,
)
from app.schemas.error import ErrorResponse
from app.services.agent_run_reads import AgentRunReadService, InvalidAgentRunReadError
from app.services.agent_runs import AgentRunCreateConflictError, AgentRunNotFoundError, AgentRunService
from app.services.in_process_run_coordinator import (
    InProcessRunCoordinator,
    RunAlreadyStartedError,
    RunCapacityExceededError,
    RunExecutionUnavailableError,
)
from app.services.tasks import TaskNotFoundError


router = APIRouter()


def get_agent_run_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> AgentRunService:
    return AgentRunService(session)


@router.post(
    "/tasks/{task_id}/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": AgentRunResponse, "description": "The existing queued AgentRun"},
        404: {"model": ErrorResponse, "description": "Task not found"},
        409: {"model": ErrorResponse, "description": "Task status or run history prevents creation"},
        422: {"model": ErrorResponse, "description": "Invalid path or body"},
    },
)
async def create_agent_run(
    task_id: UUID,
    response: Response,
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
    payload: AgentRunCreate | None = None,
) -> AgentRunResponse:
    try:
        run, created = await service.create_initial_run(task_id)
    except TaskNotFoundError as exc:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found") from exc
    except AgentRunCreateConflictError as exc:
        raise ApiError(409, "AGENT_RUN_CREATE_CONFLICT", "Task cannot create a new AgentRun") from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return AgentRunResponse.model_validate(run, from_attributes=True)


@router.get(
    "/agent-runs/{run_id}",
    response_model=AgentRunResponse,
    responses={
        404: {"model": ErrorResponse, "description": "AgentRun not found"},
        422: {"model": ErrorResponse, "description": "Invalid run ID"},
    },
)
async def get_agent_run(
    run_id: UUID,
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
) -> AgentRunResponse:
    try:
        run = await service.get_metadata(run_id)
    except AgentRunNotFoundError as exc:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from exc
    return AgentRunResponse.model_validate(run, from_attributes=True)


def get_run_coordinator(request: Request) -> InProcessRunCoordinator | None:
    return getattr(request.app.state, "run_coordinator", None)


def _execution_unavailable(reason: str = "") -> ApiError:
    message = "Local AgentRun execution is unavailable"
    return ApiError(503, "RUN_EXECUTION_UNAVAILABLE", f"{message}: {reason}" if reason else message)


@router.post(
    "/agent-runs/{run_id}/start",
    response_model=AgentRunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"headers": {"Location": {"schema": {"type": "string"}, "description": "AgentRun metadata URI"}}},
        404: {"model": ErrorResponse, "description": "AgentRun not found"},
        409: {"model": ErrorResponse, "description": "AgentRun already accepted or started"},
        422: {"model": ErrorResponse, "description": "Invalid run ID or body"},
        429: {"model": ErrorResponse, "description": "Local execution capacity exhausted"},
        503: {"model": ErrorResponse, "description": "Trusted local execution is unavailable"},
    },
)
async def start_agent_run(
    run_id: UUID,
    response: Response,
    coordinator: Annotated[InProcessRunCoordinator | None, Depends(get_run_coordinator)],
    payload: AgentRunCommand | None = None,
) -> AgentRunStartResponse:
    if coordinator is None:
        raise _execution_unavailable()
    try:
        await coordinator.start(run_id)
    except AgentRunNotFoundError as exc:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from exc
    except RunAlreadyStartedError as exc:
        raise ApiError(409, "RUN_ALREADY_STARTED", "AgentRun already accepted or started") from exc
    except RunCapacityExceededError as exc:
        raise ApiError(429, "RUN_CAPACITY_EXCEEDED", "Local execution capacity exhausted") from exc
    except RunExecutionUnavailableError as exc:
        raise _execution_unavailable(str(exc)) from exc
    response.headers["Location"] = f"/agent-runs/{run_id}"
    return AgentRunStartResponse(run_id=run_id, accepted=True)


@router.post(
    "/agent-runs/{run_id}/cancel",
    response_model=AgentRunResponse,
    responses={
        404: {"model": ErrorResponse, "description": "AgentRun not found"},
        409: {"model": ErrorResponse, "description": "AgentRun cannot be cancelled"},
        422: {"model": ErrorResponse, "description": "Invalid run ID or body"},
        503: {"model": ErrorResponse, "description": "Trusted local execution is unavailable"},
    },
)
async def cancel_agent_run(
    run_id: UUID,
    coordinator: Annotated[InProcessRunCoordinator | None, Depends(get_run_coordinator)],
    payload: AgentRunCommand | None = None,
) -> AgentRunResponse:
    if coordinator is None:
        raise _execution_unavailable()
    try:
        run = await coordinator.cancel(run_id)
    except AgentRunNotFoundError as exc:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from exc
    except InvalidAgentRunTransition as exc:
        raise ApiError(409, "RUN_NOT_CANCELLABLE", "AgentRun cannot be cancelled") from exc
    except RunExecutionUnavailableError as exc:
        raise _execution_unavailable() from exc
    return AgentRunResponse.model_validate(run, from_attributes=True)


_READ_RESPONSES = {
    404: {"model": ErrorResponse, "description": "AgentRun not found"},
    422: {"model": ErrorResponse, "description": "Invalid run ID or query"},
    500: {"model": ErrorResponse, "description": "AgentRun data is unavailable"},
}


def get_run_read_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> AgentRunReadService:
    return AgentRunReadService(session)


def _integer_query(value: object) -> object:
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is None:
        raise ValueError("Expected an integer")
    return value


@router.get("/agent-runs/{run_id}/events", response_model=AgentRunEventsResponse, responses=_READ_RESPONSES)
async def get_agent_run_events(
    run_id: UUID,
    service: Annotated[AgentRunReadService, Depends(get_run_read_service)],
    after_sequence: Annotated[int, Field(ge=0, le=MAX_EVENT_SEQUENCE), BeforeValidator(_integer_query), Query()] = 0,
    limit: Annotated[int, Field(ge=1, le=100), BeforeValidator(_integer_query), Query()] = 50,
) -> AgentRunEventsResponse:
    try:
        return await service.read_events(run_id, after_sequence, limit)
    except AgentRunNotFoundError:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from None
    except (SQLAlchemyError, InvalidAgentRunReadError):
        raise ApiError(500, "AGENT_RUN_READ_FAILED", "AgentRun data is unavailable") from None


@router.get("/agent-runs/{run_id}/result", response_model=AgentRunResultResponse, responses=_READ_RESPONSES)
async def get_agent_run_result(
    run_id: UUID,
    service: Annotated[AgentRunReadService, Depends(get_run_read_service)],
) -> AgentRunResultResponse:
    try:
        return await service.read_result(run_id)
    except AgentRunNotFoundError:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found") from None
    except (SQLAlchemyError, InvalidAgentRunReadError):
        raise ApiError(500, "AGENT_RUN_READ_FAILED", "AgentRun data is unavailable") from None
