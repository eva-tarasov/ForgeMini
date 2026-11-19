from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors import ApiError
from app.schemas.error import ErrorData, ErrorResponse, ValidationIssue

_SAFE_FIELDS = {"name", "workspace_id", "goal", "acceptance_criteria", "task_id", "run_id", "after_sequence", "limit"}
_SAFE_LOCATIONS = {"body", "path", "query", "header", "cookie"}
_VALIDATION_MESSAGES = {
    "missing": "Field is required",
    "string_type": "Expected a string",
    "list_type": "Expected a list",
    "uuid_type": "Invalid UUID",
    "uuid_parsing": "Invalid UUID",
    "string_too_short": "String is too short",
    "string_too_long": "String is too long",
    "too_short": "Too few items",
    "too_long": "Too many items",
    "json_invalid": "Invalid JSON",
}


def _safe_field(location: Sequence[str | int]) -> str:
    if not location or location[0] not in _SAFE_LOCATIONS:
        return "request"

    parts = [location[0]]
    for part in location[1:]:
        if isinstance(part, int) and part >= 0:
            parts.append(str(part))
        elif isinstance(part, str) and part in _SAFE_FIELDS:
            parts.append(part)
        else:
            break
    return ".".join(parts)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ValidationIssue] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorData(code=code, message=message, details=details or [])
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.message)


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        ValidationIssue(
            field=_safe_field(error.get("loc", ())),
            message=_VALIDATION_MESSAGES.get(error.get("type"), "Invalid value"),
        )
        for error in exc.errors()
    ]
    return _error_response(422, "VALIDATION_ERROR", "Request validation failed", details)


async def _http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    if exc.status_code == 404:
        return _error_response(404, "NOT_FOUND", "Not found", headers=exc.headers)
    if exc.status_code == 405:
        return _error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed", headers=exc.headers)
    return _error_response(exc.status_code, "HTTP_ERROR", "HTTP error", headers=exc.headers)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
