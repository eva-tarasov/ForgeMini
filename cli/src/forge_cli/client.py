"""Synchronous, explicitly owned HTTP client for the Forge API."""

from collections.abc import Callable
from types import TracebackType
from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from forge_cli.config import resolve_base_url
from forge_cli.dto import (
    MAX_EVENT_PAGE_SIZE,
    MAX_EVENT_SEQUENCE,
    AgentRunEventsResponse,
    AgentRunResponse,
    AgentRunResultResponse,
    AgentRunStartResponse,
    ErrorResponse,
    HealthResponse,
    RunReport,
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

    def create_run(self, task_id: UUID) -> AgentRunResponse:
        if not isinstance(task_id, UUID):
            raise TypeError("task_id must be a UUID.")
        return self._request(
            "POST",
            "/tasks/{task_id}/runs",
            201,
            TypeAdapter(AgentRunResponse),
            path=f"/tasks/{task_id}/runs",
            additional_success_statuses=(200,),
            response_matches=lambda run: (
                run.task_id == task_id and run.attempt_number == 1 and run.status == "queued"
            ),
        )

    def get_run(self, run_id: UUID) -> AgentRunResponse:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        return self._request(
            "GET",
            "/agent-runs/{run_id}",
            200,
            TypeAdapter(AgentRunResponse),
            path=f"/agent-runs/{run_id}",
            response_matches=lambda run: run.id == run_id,
        )

    def start_run(self, run_id: UUID) -> AgentRunStartResponse:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        return self._request(
            "POST",
            "/agent-runs/{run_id}/start",
            202,
            TypeAdapter(AgentRunStartResponse),
            path=f"/agent-runs/{run_id}/start",
            response_matches=lambda response: response.run_id == run_id,
        )

    def cancel_run(self, run_id: UUID) -> AgentRunResponse:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        return self._request(
            "POST",
            "/agent-runs/{run_id}/cancel",
            200,
            TypeAdapter(AgentRunResponse),
            path=f"/agent-runs/{run_id}/cancel",
            response_matches=lambda run: run.id == run_id and run.status == "cancelled",
        )

    def get_run_events(
        self, run_id: UUID, *, after_sequence: int = 0, limit: int = 50
    ) -> AgentRunEventsResponse:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        if type(after_sequence) is not int or not 0 <= after_sequence <= MAX_EVENT_SEQUENCE:
            raise ValueError(f"after_sequence must be an integer between 0 and {MAX_EVENT_SEQUENCE}.")
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise ValueError(f"limit must be an integer between 1 and {MAX_EVENT_PAGE_SIZE}.")

        def page_matches(page: AgentRunEventsResponse) -> bool:
            # The backend reads one extra event to determine has_more.
            if len(page.items) > limit or (
                page.has_more
                and (len(page.items) != limit or page.next_after_sequence == MAX_EVENT_SEQUENCE)
            ):
                return False
            previous = after_sequence
            for item in page.items:
                if item.sequence <= previous:
                    return False
                previous = item.sequence
            return page.next_after_sequence == previous

        return self._request(
            "GET",
            "/agent-runs/{run_id}/events",
            200,
            TypeAdapter(AgentRunEventsResponse),
            path=f"/agent-runs/{run_id}/events",
            params={"after_sequence": after_sequence, "limit": limit},
            response_matches=page_matches,
        )

    def get_run_report(self, run_id: UUID) -> RunReport:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        return self._request(
            "GET", "/agent-runs/{run_id}/report", 200, TypeAdapter(RunReport),
            path=f"/agent-runs/{run_id}/report", response_matches=lambda report: report.run_id == run_id,
        )

    def get_run_patch(self, run_id: UUID) -> str:
        """The unified diff as text (plain-text endpoint, so no JSON adapter)."""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        endpoint = "/agent-runs/{run_id}/patch"
        try:
            response = self._client.request("GET", f"/agent-runs/{run_id}/patch")
        except httpx.TimeoutException:
            raise ForgeTransportError("GET", endpoint, "timeout") from None
        except httpx.ConnectError:
            raise ForgeTransportError("GET", endpoint, "connection") from None
        except httpx.RequestError:
            raise ForgeTransportError("GET", endpoint, "transport_failure") from None
        if response.status_code != 200:
            raise ForgeAPIError("GET", endpoint, response.status_code, 200, None)
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ForgeResponseError("GET", endpoint, response.status_code) from None

    def get_run_result(self, run_id: UUID) -> AgentRunResultResponse:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID.")
        return self._request(
            "GET",
            "/agent-runs/{run_id}/result",
            200,
            TypeAdapter(AgentRunResultResponse),
            path=f"/agent-runs/{run_id}/result",
            response_matches=lambda result: result.run_id == run_id,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        expected_status: int,
        adapter: TypeAdapter[T],
        *,
        additional_success_statuses: tuple[int, ...] = (),
        path: str | None = None,
        params: dict[str, int] | None = None,
        payload: BaseModel | None = None,
        headers: dict[str, str] | None = None,
        response_matches: Callable[[T], bool] | None = None,
    ) -> T:
        try:
            response = self._client.request(
                method,
                path if path is not None else endpoint,
                params=params,
                json=payload.model_dump(mode="json") if payload is not None else None,
                headers=headers,
            )
        except httpx.TimeoutException:
            raise ForgeTransportError(method, endpoint, "timeout") from None
        except httpx.ConnectError:
            raise ForgeTransportError(method, endpoint, "connection") from None
        except httpx.RequestError:
            raise ForgeTransportError(method, endpoint, "transport_failure") from None

        if response.status_code not in (expected_status, *additional_success_statuses):
            server_code = None
            try:
                envelope = ErrorResponse.model_validate_json(response.content, strict=True)
            except ValidationError:
                pass
            else:
                server_code = envelope.error.code
            raise ForgeAPIError(method, endpoint, response.status_code, expected_status, server_code)

        try:
            result = adapter.validate_json(response.content, strict=True)
        except ValidationError:
            raise ForgeResponseError(method, endpoint, response.status_code) from None

        if response_matches is not None and not response_matches(result):
            error = ForgeResponseError(method, endpoint, response.status_code)
            error.args = (f"{error} Received HTTP {error.status_code}.",)
            raise error

        return result
