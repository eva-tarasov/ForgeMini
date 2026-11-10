import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.logging import JsonFormatter
from fake_app import create_test_app as create_app


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    handler = CaptureHandler()
    logger = logging.getLogger("forge.http")
    logger.addHandler(handler)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)


def parsed_log(line: str) -> dict[str, object]:
    entry = json.loads(line)
    assert set(entry) == {
        "timestamp", "level", "event", "request_id", "method", "route",
        "status_code", "duration_ms",
    }
    assert datetime.fromisoformat(entry["timestamp"]).tzinfo == timezone.utc
    assert entry["level"] == "INFO"
    assert entry["event"] == "http_request_completed"
    assert UUID(entry["request_id"])
    assert isinstance(entry["duration_ms"], (int, float))
    assert entry["duration_ms"] >= 0
    assert "\n" not in line
    return entry


def test_health_logs_once_and_returns_request_id(captured_logs: list[str]) -> None:
    with TestClient(create_app()) as client:
        first = client.get("/health")
        second = client.get("/health")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"status": "ok"}
    assert len(captured_logs) == 2
    entries = [parsed_log(line) for line in captured_logs]
    assert all(entry["route"] == "/health" for entry in entries)
    assert all(entry["method"] == "GET" for entry in entries)
    assert all(entry["status_code"] == 200 for entry in entries)
    assert first.headers["x-request-id"] == entries[0]["request_id"]
    assert second.headers["x-request-id"] == entries[1]["request_id"]
    assert entries[0]["request_id"] != entries[1]["request_id"]


def test_workspace_and_task_log_status_and_route(captured_logs: list[str]) -> None:
    with TestClient(create_app()) as client:
        workspace = client.post("/workspaces", json={"name": "Example"})
        task = client.post(
            "/tasks",
            json={
                "workspace_id": workspace.json()["id"],
                "goal": "Build API",
                "acceptance_criteria": ["Works"],
            },
        )
        fetched = client.get(f"/tasks/{task.json()['id']}")

    assert [workspace.status_code, task.status_code, fetched.status_code] == [201, 201, 200]
    assert len(captured_logs) == 3
    entries = [parsed_log(line) for line in captured_logs]
    assert [(entry["method"], entry["route"], entry["status_code"]) for entry in entries] == [
        ("POST", "/workspaces", 201),
        ("POST", "/tasks", 201),
        ("GET", "/tasks/{task_id}", 200),
    ]
    for response, entry in zip((workspace, task, fetched), entries):
        assert response.headers["x-request-id"] == entry["request_id"]


def test_handled_errors_keep_contract_and_request_id(captured_logs: list[str]) -> None:
    with TestClient(create_app()) as client:
        missing = client.get(f"/tasks/{uuid4()}")
        invalid = client.get("/tasks/not-a-uuid")
        wrong_method = client.get("/tasks")

    assert [missing.status_code, invalid.status_code, wrong_method.status_code] == [404, 422, 405]
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert wrong_method.headers["allow"] == "POST"
    assert len(captured_logs) == 3
    entries = [parsed_log(line) for line in captured_logs]
    assert [entry["route"] for entry in entries[:2]] == ["/tasks/{task_id}"] * 2
    for response, entry in zip((missing, invalid, wrong_method), entries):
        assert response.headers["x-request-id"] == entry["request_id"]
        assert entry["status_code"] == response.status_code


def test_unknown_route_and_secrets_are_not_logged(captured_logs: list[str]) -> None:
    secret = "private-secret-value"
    supplied_id = str(uuid4())
    with TestClient(create_app()) as client:
        client.cookies.set("session", secret)
        response = client.get(
            f"/missing/{secret}?token={secret}",
            headers={"Authorization": f"Bearer {secret}", "X-Request-ID": supplied_id},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert len(captured_logs) == 1
    entry = parsed_log(captured_logs[0])
    assert entry["route"] == "unmatched"
    assert entry["request_id"] == response.headers["x-request-id"]
    assert entry["request_id"] != supplied_id
    assert secret not in captured_logs[0]
    assert "token=" not in captured_logs[0]
    assert "Authorization" not in captured_logs[0]
    assert "session" not in captured_logs[0]


def test_repeated_create_app_does_not_duplicate_events(captured_logs: list[str]) -> None:
    first_app = create_app()
    second_app = create_app()
    with TestClient(first_app) as first, TestClient(second_app) as second:
        first.get("/health")
        second.get("/health")

    assert len(captured_logs) == 2
    logger = logging.getLogger("forge.http")
    assert sum(handler.name == "forge.http.json" for handler in logger.handlers) == 1


def test_unexpected_error_is_logged_and_raised(captured_logs: list[str]) -> None:
    app = create_app()

    @app.get("/broken")
    def broken() -> None:
        raise RuntimeError("private-exception-value")

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="private-exception-value"):
            client.get("/broken")

    assert len(captured_logs) == 1
    entry = parsed_log(captured_logs[0])
    assert entry["route"] == "/broken"
    assert entry["status_code"] == 500
    assert "private-exception-value" not in captured_logs[0]
