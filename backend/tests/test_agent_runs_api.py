from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_session
from app.main import create_app
from app.services.agent_runs import AgentRunNotFoundError, AgentRunService
from app.services.tasks import TaskNotFoundError


@pytest.fixture
def api(monkeypatch):
    def unexpected_session():
        pytest.fail("Unit HTTP checks must use the overridden dependency")

    app = create_app(session_factory=unexpected_session)

    async def test_session():
        yield object()

    app.dependency_overrides[get_session] = test_session
    metadata = SimpleNamespace(
        id=uuid4(), task_id=uuid4(), attempt_number=1, status="queued",
        created_at=datetime(2026, 11, 19, 8, tzinfo=UTC),
        final_result={"private": "synthetic secret"}, fixture_root="/private/synthetic-fixture",
    )
    create = AsyncMock(return_value=(metadata, True))
    get = AsyncMock(return_value=metadata)
    monkeypatch.setattr(AgentRunService, "create_initial_run", create, raising=False)
    monkeypatch.setattr(AgentRunService, "get_metadata", get, raising=False)
    with TestClient(app) as client:
        yield client, metadata, create, get


def test_create_get_and_queued_replay_have_exact_metadata(api):
    client, run, create, get = api
    created = client.post(f"/tasks/{run.task_id}/runs")
    assert created.status_code == 201
    expected = {
        "id": str(run.id), "task_id": str(run.task_id), "attempt_number": 1,
        "status": "queued", "created_at": "2026-11-19T08:00:00Z",
    }
    assert created.json() == expected
    fetched = client.get(f"/agent-runs/{run.id}")
    assert fetched.status_code == 200 and fetched.json() == expected
    create.return_value = (run, False)
    replay = client.post(f"/tasks/{run.task_id}/runs", json={})
    assert replay.status_code == 200 and replay.json() == expected
    get.assert_awaited_once_with(run.id)
    assert create.await_count == 2


@pytest.mark.parametrize(
    "method,path,field", [("POST", "/tasks/not-a-uuid/runs", "task_id"), ("GET", "/agent-runs/not-a-uuid", "run_id")],
)
def test_agent_run_paths_reject_invalid_uuids(api, method, path, field):
    client, _, create, get = api
    response = client.request(method, path)
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "VALIDATION_ERROR", "message": "Request validation failed",
        "details": [{"field": f"path.{field}", "message": "Invalid UUID"}],
    }
    create.assert_not_awaited()
    get.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("status", "succeeded"), ("attempt_number", 7), ("fixture_root", "/private/synthetic-root"),
    ("synthetic-credential-field", "synthetic credential"),
])
def test_create_rejects_client_owned_fields_without_echoing_them(api, field, value):
    client, run, create, _ = api
    response = client.post(f"/tasks/{run.task_id}/runs", json={field: value})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["field"] == "body"
    assert field not in response.text and str(value) not in response.text
    create.assert_not_awaited()


def test_create_and_get_map_absent_resources(api):
    client, run, create, get = api
    create.side_effect = TaskNotFoundError(run.task_id)
    get.side_effect = AgentRunNotFoundError(run.id)
    for response, code in (
        (client.post(f"/tasks/{run.task_id}/runs"), "TASK_NOT_FOUND"),
        (client.get(f"/agent-runs/{run.id}"), "AGENT_RUN_NOT_FOUND"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["details"] == []


def test_agent_run_openapi_matches_actual_request_and_response_contract(api):
    client, _, _, _ = api
    schema = client.get("/openapi.json").json()
    create = schema["paths"]["/tasks/{task_id}/runs"]["post"]
    get = schema["paths"]["/agent-runs/{run_id}"]["get"]
    assert set(create["responses"]) == {"200", "201", "404", "409", "422"}
    assert set(get["responses"]) == {"200", "404", "422"}
    for route, statuses in ((create, {"200", "201"}), (get, {"200"})):
        assert route["parameters"][0]["schema"]["format"] == "uuid"
        for status, response in route["responses"].items():
            ref = response["content"]["application/json"]["schema"]["$ref"]
            assert ref == f"#/components/schemas/{'AgentRunResponse' if status in statuses else 'ErrorResponse'}"
    assert not create["requestBody"].get("required", False)
    schemas = schema["components"]["schemas"]
    assert {"$ref": "#/components/schemas/AgentRunCreate"} in (
        create["requestBody"]["content"]["application/json"]["schema"]["anyOf"]
    )
    assert schemas["AgentRunCreate"]["additionalProperties"] is False
    assert schemas["AgentRunCreate"]["properties"] == {}
    assert set(schemas["AgentRunResponse"]["properties"]) == {"id", "task_id", "attempt_number", "status", "created_at"}
    assert schemas["AgentRunResponse"]["properties"]["created_at"]["format"] == "date-time"
