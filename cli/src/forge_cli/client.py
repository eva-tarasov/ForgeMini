"""Synchronous, explicitly owned HTTP client for the Forge API."""

from types import TracebackType
from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from forge_cli.config import resolve_base_url
from forge_cli.dto import (
    ErrorResponse,
    HealthResponse,
    TaskCreate,
    TaskResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)
from forge_cli.errors import ForgeAPIError, ForgeResponseError, ForgeTransportError

T = TypeVar("T")


class ForgeAPIClient:
    """Own the connection pool and injected transport. Use with a with-statement."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(base_url, str):
            raise ValueError("An explicit base URL is required.")
        validated_url = resolve_base_url(base_url)
        try:
            self._client = httpx.Client(
                base_url=validated_url,
                transport=transport,
                timeout=httpx.Timeout(5.0, connect=2.0),
                follow_redirects=False,
                trust_env=False,
            )
        except (httpx.InvalidURL, ValueError):
            raise ValueError("Invalid Forge base URL.") from None

    def __enter__(self) -> "ForgeAPIClient":
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    def close(self) -> None:
        self._client.close()

    def get_health(self) -> HealthResponse:
        return self._request("GET", "/health", 200, TypeAdapter(HealthResponse))

    def list_workspaces(self) -> list[WorkspaceResponse]:
        return self._request("GET", "/workspaces", 200, TypeAdapter(list[WorkspaceResponse]))

    def create_workspace(self, payload: WorkspaceCreate) -> WorkspaceResponse:
        return self._request(
            "POST", "/workspaces", 201, TypeAdapter(WorkspaceResponse), payload=payload
        )

    def create_task(self, payload: TaskCreate, *, idempotency_key: UUID | None = None) -> TaskResponse:
        headers = {}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, UUID):
                raise TypeError("idempotency_key must be a UUID.")
            headers["Idempotency-Key"] = str(idempotency_key)
        return self._request(
            "POST", "/tasks", 201, TypeAdapter(TaskResponse), payload=payload, headers=headers
        )

    def get_task(self, task_id: UUID) -> TaskResponse:
        if not isinstance(task_id, UUID):
            raise TypeError("task_id must be a UUID.")
        return self._request(
            "GET", "/tasks/{task_id}", 200, TypeAdapter(TaskResponse), path=f"/tasks/{task_id}"
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        expected_status: int,
        adapter: TypeAdapter[T],
        *,
        path: str | None = None,
        payload: BaseModel | None = None,
        headers: dict[str, str] | None = None,
    ) -> T:
        try:
            response = self._client.request(
                method,
                path if path is not None else endpoint,
                json=payload.model_dump(mode="json") if payload is not None else None,
                headers=headers,
            )
        except httpx.TimeoutException:
            raise ForgeTransportError(method, endpoint, "timeout") from None
        except httpx.ConnectError:
            raise ForgeTransportError(method, endpoint, "connection") from None
        except httpx.RequestError:
            raise ForgeTransportError(method, endpoint, "transport_failure") from None

        if response.status_code != expected_status:
            server_code = None
            try:
                envelope = ErrorResponse.model_validate_json(response.content, strict=True)
            except ValidationError:
                pass
            else:
                server_code = envelope.error.code
            raise ForgeAPIError(method, endpoint, response.status_code, expected_status, server_code)

        try:
            return adapter.validate_json(response.content, strict=True)
        except ValidationError:
            raise ForgeResponseError(method, endpoint, response.status_code) from None
