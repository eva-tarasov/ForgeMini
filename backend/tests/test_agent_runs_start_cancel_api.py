from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    def unexpected_session():
        pytest.fail("An unconfigured or malformed request must not open a SQL session")

    with TestClient(create_app(session_factory=unexpected_session)) as client:
        yield client


@pytest.mark.parametrize("command", ["start", "cancel"])
def test_run_commands_fail_closed_without_trusted_execution(client, command):
    response = client.post(f"/agent-runs/{uuid4()}/{command}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUN_EXECUTION_UNAVAILABLE"


@pytest.mark.parametrize("command", ["start", "cancel"])
def test_run_commands_reject_malformed_uuid_and_private_fields(client, command):
    invalid = client.post(f"/agent-runs/not-a-uuid/{command}")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["details"] == [{"field": "path.run_id", "message": "Invalid UUID"}]
    invalid = client.post(f"/agent-runs/{uuid4()}/{command}", json={
        "fixture_root": "/private/synthetic-fixture",
        "api_key": "synthetic-secret",
        "permissions": ["repository.write"],
        "approval": "approve",
        "runner_type": "synthetic-runner",
    })
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert {issue["field"] for issue in invalid.json()["error"]["details"]} == {"body"}
    assert all(value not in invalid.text for value in ("fixture_root", "synthetic-fixture", "synthetic-secret", "approve"))


def test_run_command_openapi_matches_http_contract(client):
    schema = client.get("/openapi.json").json()
    start = schema["paths"]["/agent-runs/{run_id}/start"]["post"]
    cancel = schema["paths"]["/agent-runs/{run_id}/cancel"]["post"]
    assert set(start["responses"]) == {"202", "404", "409", "422", "429", "503"}
    assert set(cancel["responses"]) == {"200", "404", "409", "422", "503"}
    assert start["responses"]["202"]["headers"]["Location"]["schema"]["type"] == "string"
    for operation, success, model in ((start, "202", "AgentRunStartResponse"), (cancel, "200", "AgentRunResponse")):
        assert operation["parameters"][0]["schema"]["format"] == "uuid"
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert {"$ref": "#/components/schemas/AgentRunCommand"} in body["anyOf"]
        for status, response in operation["responses"].items():
            name = model if status == success else "ErrorResponse"
            assert response["content"]["application/json"]["schema"]["$ref"] == f"#/components/schemas/{name}"
    assert schema["components"]["schemas"]["AgentRunCommand"]["additionalProperties"] is False
    accepted = schema["components"]["schemas"]["AgentRunStartResponse"]
    assert set(accepted["required"]) == {"run_id", "accepted"}
    assert accepted["properties"]["accepted"]["const"] is True
