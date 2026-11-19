import json
import logging
from contextlib import asynccontextmanager
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import JsonFormatter
from app.main import create_app
from app.services.local_approval_access import LocalApprovalAccessBinding


def test_approval_discovery_is_unavailable_without_trusted_binding():
    def no_session():
        raise AssertionError("disabled approval discovery must not open a database session")

    with TestClient(create_app(session_factory=no_session)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/approvals?status=pending")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_API_UNAVAILABLE"


TOKEN = "synthetic-local-approval-capability"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("token", [
    "",
    "x",
    "a" * 31,
    "a" * 513,
    "a" * 31 + "!",
], ids=["empty", "one-character", "below-minimum", "above-maximum", "invalid-character"])
def test_local_approval_access_rejects_invalid_token(token):
    with pytest.raises(ValueError) as error:
        LocalApprovalAccessBinding(uuid4(), SecretStr(token))

    assert error.value.args == ("Invalid local approval access token",)


@pytest.mark.parametrize("generate_token", [
    lambda: "a" * 32,
    lambda: "a" * 512,
    lambda: token_urlsafe(32),
], ids=["minimum", "maximum", "random"])
def test_local_approval_access_accepts_valid_token_without_exposing_it(generate_token):
    token = generate_token()
    binding = LocalApprovalAccessBinding(uuid4(), SecretStr(token))

    assert binding.accepts(token)
    assert not binding.accepts(token + "x")
    assert token not in repr(binding)


@pytest.fixture
def binding():
    return LocalApprovalAccessBinding(uuid4(), SecretStr(TOKEN))


@pytest.fixture
def http_logs():
    records: list[logging.LogRecord] = []

    class RecordCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = RecordCollector()
    logger = logging.getLogger("forge.http")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        handler.close()


def assert_approval_http_logs(records, *expected_statuses):
    fields = {"request_id", "method", "route", "status_code", "duration_ms"}
    formatter = JsonFormatter()
    rendered = [formatter.format(record) for record in records]
    entries = [json.loads(entry) for entry in rendered]
    assert [entry["status_code"] for entry in entries] == list(expected_statuses)
    for record, entry in zip(records, entries, strict=True):
        assert record.getMessage() == "http_request_completed"
        assert set(record.http_fields) == fields
        assert set(entry) == fields | {"timestamp", "level", "event"}
        assert (entry["method"], entry["route"], entry["level"]) == (
            "GET", "/agent-runs/{run_id}/approvals", "INFO",
        )
    return "\n".join(rendered) + repr([record.__dict__ for record in records])


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer synthetic-invalid-token"},
    {"Authorization": f"Basic {TOKEN}"},
    [("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer other-token")],
], ids=["missing", "wrong-token", "wrong-scheme", "duplicate"])
def test_denied_capability_never_opens_database(binding, headers, http_logs):
    def no_session():
        raise AssertionError("denied approval discovery must not open a database session")

    app = create_app(session_factory=no_session, approval_access_binding=binding)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        responses = [client.get(f"/agent-runs/{uuid4()}/approvals", headers=headers) for _ in range(2)]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"error": {
            "code": "APPROVAL_ACCESS_DENIED", "message": "Approval access denied", "details": [],
        }}
    logged = assert_approval_http_logs(http_logs, 403, 403).lower()
    for sentinel in (TOKEN, "synthetic-invalid-token", "other-token", "authorization", "credential_length"):
        assert sentinel not in logged
        assert all(sentinel not in response.text.lower() for response in responses)


@pytest.mark.parametrize("run_id,query", [
    ("synthetic-malformed-id", ""),
    (str(uuid4()), "?status=synthetic-malformed-status"),
    (str(uuid4()), f"?workspace_id={uuid4()}"),
    (str(uuid4()), "?limit=999"),
], ids=["uuid", "status", "workspace-override", "limit-override"])
def test_approval_discovery_rejects_malformed_requests(binding, run_id, query):
    execute = AsyncMock()

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=execute)

    with TestClient(create_app(session_factory=session, approval_access_binding=binding)) as client:
        response = client.get(f"/agent-runs/{run_id}/approvals{query}", headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "synthetic-malformed" not in response.text
    execute.assert_not_awaited()


def test_approval_discovery_sanitizes_sql_failure(binding, http_logs):
    driver_detail = "synthetic-private-sql-driver-detail"

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(side_effect=SQLAlchemyError(driver_detail)))

    with TestClient(create_app(session_factory=session, approval_access_binding=binding)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/approvals", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "APPROVAL_READ_FAILED", "message": "Approval data is unavailable", "details": [],
    }}
    logged = assert_approval_http_logs(http_logs, 500)
    assert not any(marker in response.text + logged for marker in (TOKEN, driver_detail))


def test_approval_discovery_openapi_matches_public_contract():
    schema = create_app().openapi()
    [method] = schema["paths"]["/agent-runs/{run_id}/approvals"].values()
    schemas = schema["components"]["schemas"]
    assert set(method["responses"]) == {"200", "403", "404", "422", "500", "503"}
    assert method["security"] == [{"LocalApprovalCapability": []}]
    assert schema["components"]["securitySchemes"]["LocalApprovalCapability"] == {
        "type": "http", "scheme": "bearer",
    }
    assert [(parameter["name"], parameter["in"]) for parameter in method["parameters"]] == [
        ("run_id", "path"), ("status", "query"),
    ]
    assert method["parameters"][1]["schema"]["const"] == "pending"
    assert set(schemas["PendingApprovalResponse"]["properties"]) == {"id", "run_id", "tool_name", "status", "requested_at"}
    assert all(response["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
               for status, response in method["responses"].items() if status != "200")
    assert "security" not in schema["paths"]["/agent-runs/{run_id}"]["get"]
