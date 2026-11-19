from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sqlalchemy.exc import SQLAlchemyError
from test_approval_review_api import REVIEW_AUTH, REVIEW_TOKEN, review_binding
from test_approvals_api import AUTH, TOKEN, http_logs

from app.core.logging import JsonFormatter
from app.main import create_app
from app.repositories.agent_events import AgentEventRepository


def test_evidence_list_is_fail_closed_without_read_binding():
    def no_session():
        raise AssertionError("Disabled evidence API must not open a session")

    with TestClient(create_app(session_factory=no_session)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/evidence")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_API_UNAVAILABLE"


LIST_PATH = "/agent-runs/{run_id}/evidence"
DETAIL_PATH = LIST_PATH + "/{attempt_id}"


def evidence_url(run_id, attempt_id=None):
    return LIST_PATH.format(run_id=run_id) if attempt_id is None else DETAIL_PATH.format(
        run_id=run_id, attempt_id=attempt_id,
    )


@asynccontextmanager
async def _session():
    yield object()


@pytest.mark.parametrize("path", ["list", "detail"])
@pytest.mark.parametrize("headers", [
    {}, REVIEW_AUTH, {"Authorization": "Bearer synthetic-invalid-evidence-capability"},
    [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {REVIEW_TOKEN}")],
], ids=["missing", "decision-token", "invalid", "duplicate"])
def test_evidence_rejects_capability_before_opening_session(review_binding, http_logs, path, headers):
    factory = Mock(side_effect=AssertionError("Denied evidence request must not open a session"))
    app = create_app(session_factory=factory, approval_access_binding=review_binding)
    with TestClient(app) as client:
        response = client.get(evidence_url(uuid4(), uuid4() if path == "detail" else None), headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "APPROVAL_ACCESS_DENIED"
    factory.assert_not_called()
    [record] = http_logs
    assert record.http_fields["route"] == (DETAIL_PATH if path == "detail" else LIST_PATH)
    captured = response.text + JsonFormatter().format(record) + repr(record.__dict__)
    assert all(secret not in captured for secret in (TOKEN, REVIEW_TOKEN, "synthetic-invalid-evidence-capability"))


@pytest.mark.parametrize("invalid", ["run", "attempt", "cursor", "decimal", "limit-low", "limit-high", "sequence-overflow"])
def test_evidence_rejects_invalid_identifiers_and_query(review_binding, monkeypatch, invalid):
    read = AsyncMock()
    monkeypatch.setattr(AgentEventRepository, "read_result_snapshot", read)
    run_id, attempt_id, query = uuid4(), None, {}
    if invalid == "run":
        run_id = "synthetic-private-invalid-run"
    elif invalid == "attempt":
        attempt_id = "synthetic-private-invalid-attempt"
    else:
        query = {
            "cursor": {"after_sequence": -1}, "decimal": {"after_sequence": "1.0"},
            "limit-low": {"limit": 0}, "limit-high": {"limit": 21},
            "sequence-overflow": {"after_sequence": 2_147_483_648},
        }[invalid]
    app = create_app(session_factory=_session, approval_access_binding=review_binding)
    with TestClient(app) as client:
        response = client.get(evidence_url(run_id, attempt_id), headers=AUTH, params=query)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "synthetic-private" not in response.text
    read.assert_not_awaited()


@pytest.mark.parametrize("path", ["list", "detail"])
def test_evidence_sanitizes_sql_and_internal_read_errors(review_binding, monkeypatch, http_logs, path):
    secret = "synthetic-private-sql-error /host/private"
    exception = SQLAlchemyError(secret) if path == "list" else RuntimeError(secret)
    monkeypatch.setattr(AgentEventRepository, "read_result_snapshot", AsyncMock(side_effect=exception))
    app = create_app(session_factory=_session, approval_access_binding=review_binding)
    with TestClient(app) as client:
        response = client.get(evidence_url(uuid4(), uuid4() if path == "detail" else None), headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "VERIFICATION_EVIDENCE_READ_FAILED", "message": "Verification evidence read failed", "details": [],
    }}
    [log] = http_logs
    assert secret not in response.text + JsonFormatter().format(log) + repr(log.__dict__)


def test_evidence_openapi_reuses_read_capability_and_documents_allowlisted_responses():
    contract = create_app().openapi()
    summary_fields = {"attempt_id", "sequence", "verdict", "file_verdict", "audit_complete",
                      "isolated_fixture", "check_count"}
    schemas = contract["components"]["schemas"]
    assert set(schemas["EvidenceAttemptSummary"]["properties"]) == summary_fields
    assert set(schemas["EvidenceAttemptDetail"]["properties"]) == summary_fields | {"checks"}
    assert set(schemas["EvidenceCheckResponse"]["properties"]) == {"check_id", "criterion_index", "path", "outcome"}
    assert set(schemas["EvidenceCheckResponse"]["properties"]["outcome"]["enum"]) == {
        "match", "mismatch", "read_error", "unknown_effect", "ambiguous", "missing",
    }
    assert schemas["EvidenceAttemptDetail"]["properties"]["checks"]["maxItems"] == 32
    assert set(schemas["EvidenceListResponse"]["properties"]) == {"run_id", "items", "next_after_sequence", "has_more"}
    for path, model in ((LIST_PATH, "EvidenceListResponse"), (DETAIL_PATH, "EvidenceAttemptDetail")):
        [method] = contract["paths"][path].values()
        responses = method["responses"]
        assert set(responses) == {"200", "403", "404", "422", "500", "503"}
        assert responses["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/" + model)
        assert method["security"] == [{"LocalApprovalCapability": []}]
    params = {param["name"]: param["schema"] for param in contract["paths"][LIST_PATH]["get"]["parameters"]}
    assert (params["after_sequence"]["minimum"], params["after_sequence"]["default"]) == (0, 0)
    assert (params["limit"]["minimum"], params["limit"]["maximum"], params["limit"]["default"]) == (1, 20, 20)
    assert "security" not in contract["paths"]["/agent-runs/{run_id}/result"]["get"]
