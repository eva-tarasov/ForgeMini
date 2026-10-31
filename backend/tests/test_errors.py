from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def assert_error_shape(response: Response, code: str) -> None:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["details"], list)


def test_missing_task_uses_task_not_found(client: TestClient) -> None:
    response = client.get(f"/tasks/{uuid4()}")

    assert response.status_code == 404
    assert_error_shape(response, "TASK_NOT_FOUND")
    assert response.json()["error"]["details"] == []


def test_missing_workspace_uses_workspace_not_found(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={
            "workspace_id": str(uuid4()),
            "goal": "Create task",
            "acceptance_criteria": ["Created"],
        },
    )

    assert response.status_code == 404
    assert_error_shape(response, "WORKSPACE_NOT_FOUND")


@pytest.mark.parametrize(
    ("path", "payload", "field"),
    [
        ("/tasks/not-a-uuid", None, "path.task_id"),
        ("/tasks", {"workspace_id": "not-a-uuid", "goal": "Valid", "acceptance_criteria": ["Valid"]}, "body.workspace_id"),
        ("/tasks", {"workspace_id": str(uuid4()), "goal": "   ", "acceptance_criteria": ["Valid"]}, "body.goal"),
        ("/tasks", {"workspace_id": str(uuid4()), "goal": "Valid", "acceptance_criteria": ["   "]}, "body.acceptance_criteria.0"),
    ],
)
def test_validation_error_has_safe_details(
    client: TestClient, path: str, payload: dict[str, object] | None, field: str
) -> None:
    response = client.get(path) if payload is None else client.post(path, json=payload)

    assert response.status_code == 422
    assert_error_shape(response, "VALIDATION_ERROR")
    details = response.json()["error"]["details"]
    assert details
    assert details[0]["field"] == field
    assert all(set(issue) == {"field", "message"} for issue in details)


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    secret = "private-user-value"
    response = client.post("/workspaces", json={"name": secret + "x" * 100})

    assert response.status_code == 422
    assert_error_shape(response, "VALIDATION_ERROR")
    assert response.json()["error"]["details"][0]["field"] == "body.name"
    assert secret not in response.text
    assert "input" not in response.text
    assert "ctx" not in response.text


def test_unknown_route_uses_not_found(client: TestClient) -> None:
    response = client.get("/unknown-route")

    assert response.status_code == 404
    assert_error_shape(response, "NOT_FOUND")
    assert response.json()["error"]["details"] == []


def test_wrong_method_preserves_allow_header(client: TestClient) -> None:
    response = client.get("/tasks")

    assert response.status_code == 405
    assert_error_shape(response, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "POST"


@pytest.mark.parametrize(
    ("status_code", "header", "value", "code", "message"),
    [
        (401, "WWW-Authenticate", "Bearer", "HTTP_ERROR", "HTTP error"),
        (429, "Retry-After", "60", "HTTP_ERROR", "HTTP error"),
        (404, "X-Review-Header", "preserved", "NOT_FOUND", "Not found"),
        (405, "Allow", "GET", "METHOD_NOT_ALLOWED", "Method not allowed"),
    ],
)
def test_http_exception_preserves_headers(
    status_code: int, header: str, value: str, code: str, message: str
) -> None:
    app = create_app()

    @app.get("/test-headers")
    def fail() -> None:
        raise HTTPException(status_code=status_code, headers={header: value, "X-Extra": "kept"})

    with TestClient(app) as client:
        response = client.get("/test-headers")

    assert response.status_code == status_code
    assert response.headers[header] == value
    assert response.headers["X-Extra"] == "kept"
    assert response.json() == {"error": {"code": code, "message": message, "details": []}}


def test_other_http_exception_uses_generic_code() -> None:
    app = create_app()

    @app.get("/test-http-error")
    def fail() -> None:
        raise HTTPException(status_code=403, detail="internal secret")

    with TestClient(app) as client:
        response = client.get("/test-http-error")

    assert response.status_code == 403
    assert_error_shape(response, "HTTP_ERROR")
    assert "internal secret" not in response.text
