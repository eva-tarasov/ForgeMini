from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.main import create_app


@pytest.fixture
def client():
    @asynccontextmanager
    async def unexpected_session():
        yield object()  # Validation must finish before the endpoint tries to issue SQL.

    with TestClient(create_app(session_factory=unexpected_session)) as client:
        yield client


@pytest.mark.parametrize("suffix", ["events", "result"])
def test_read_endpoints_reject_invalid_run_uuid(client, suffix):
    response = client.get(f"/agent-runs/not-a-uuid/{suffix}")
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [{"field": "path.run_id", "message": "Invalid UUID"}]


@pytest.mark.parametrize("parameter,value", [
    ("after_sequence", "-1"), ("after_sequence", "1.7"), ("after_sequence", "1.0"),
    ("after_sequence", "2147483648"),
    ("limit", "0"), ("limit", "101"), ("limit", "private-value"),
])
def test_events_reject_invalid_cursor_and_limit_without_echo(client, parameter, value):
    response = client.get(f"/agent-runs/{uuid4()}/events", params={parameter: value})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["field"] == f"query.{parameter}"
    assert "private-value" not in response.text


def test_openapi_describes_safe_event_discriminator_result_and_query_contract(client):
    schema = client.get("/openapi.json").json()
    events = schema["paths"]["/agent-runs/{run_id}/events"]["get"]
    result = schema["paths"]["/agent-runs/{run_id}/result"]["get"]
    parameters = {parameter["name"]: parameter["schema"] for parameter in events["parameters"]}
    assert (parameters["after_sequence"]["default"], parameters["after_sequence"]["minimum"],
            parameters["after_sequence"]["maximum"]) == (0, 0, 2147483647)
    assert (parameters["limit"]["default"], parameters["limit"]["minimum"], parameters["limit"]["maximum"]) == (50, 1, 100)
    for operation, name in ((events, "AgentRunEventsResponse"), (result, "AgentRunResultResponse")):
        assert set(operation["responses"]) == {"200", "404", "422", "500"}
        for status, response in operation["responses"].items():
            expected = name if status == "200" else "ErrorResponse"
            assert response["content"]["application/json"]["schema"]["$ref"] == f"#/components/schemas/{expected}"
    schemas = schema["components"]["schemas"]
    types = schemas["AgentRunEventsResponse"]["properties"]["items"]["items"]["discriminator"]["mapping"]
    assert set(types) == {
        "agent_run_status_changed", "tool_call_proposed", "tool_call_started", "tool_call_finished",
        "approval_requested", "approval_decided", "agent_result_recorded", "agent_execution_failed",
        "agent_usage_recorded", "verification_recorded", "other",
    }
    assert set(schemas["AgentRunResultResponse"]["properties"]) == {
        "run_id", "run_status", "preliminary_status", "execution_failure_code", "verification_verdict", "usage",
    }


@pytest.mark.parametrize("suffix", ["events", "result"])
def test_sql_failure_returns_safe_error_response_without_driver_details(suffix):
    class UnavailableSession:
        async def scalar(self, statement):
            raise SQLAlchemyError("private SQL statement and /host/secret DSN")

        async def execute(self, statement):
            raise SQLAlchemyError("private SQL statement and /host/secret DSN")

    @asynccontextmanager
    async def factory():
        yield UnavailableSession()

    with TestClient(create_app(session_factory=factory)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/{suffix}")
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "AGENT_RUN_READ_FAILED", "message": "AgentRun data is unavailable", "details": [],
    }}
    assert "private SQL" not in response.text and "/host/secret" not in response.text
