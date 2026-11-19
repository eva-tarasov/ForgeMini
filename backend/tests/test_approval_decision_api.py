import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_approval_details_api import _record
from test_approval_review_api import REVIEW_AUTH, REVIEW_TOKEN, review_binding
from test_approvals_api import AUTH, TOKEN, http_logs

from app.api.agent_runs import get_run_coordinator
from app.core.logging import JsonFormatter
from app.main import create_app
from app.repositories.approvals import ApprovalRepository
from app.services.approval_contracts import ApprovalDecision
from app.services.approval_decisions import ApprovalDecisionService
from app.services.approval_preview import ApprovalDecisionConflictError, ApprovalReviewIntegrityError


def test_approval_decision_is_fail_closed_without_capability():
    def no_session():
        raise AssertionError("Disabled decision must not open a database session")

    with TestClient(create_app(session_factory=no_session)) as client:
        response = client.post(f"/agent-runs/{uuid4()}/approvals/{uuid4()}/decision", json={"decision": "reject"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_DECISION_UNAVAILABLE"


DECISION_PATH = "/agent-runs/{run_id}/approvals/{approval_id}/decision"
DIGEST = "1234567890abcdef" * 4


def decision_url(run_id, approval_id):
    return DECISION_PATH.format(run_id=run_id, approval_id=approval_id)


@asynccontextmanager
async def _session():
    yield object()


@pytest.mark.parametrize("headers", [
    {},
    AUTH,
    {"Authorization": "Bearer wrong-synthetic-decision-token"},
    [("Authorization", f"Bearer {REVIEW_TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
], ids=["absent", "read-only", "wrong", "duplicate"])
def test_decision_authorization_precedes_session(review_binding, http_logs, headers):
    factory = Mock(side_effect=AssertionError("Unauthenticated decision must not open a session"))
    with TestClient(create_app(session_factory=factory, approval_access_binding=review_binding)) as client:
        response = client.post(decision_url(uuid4(), uuid4()), headers=headers, json={"decision": "reject"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "APPROVAL_ACCESS_DENIED"
    factory.assert_not_called()
    [record] = http_logs
    assert record.http_fields["route"] == DECISION_PATH
    observed = response.text + JsonFormatter().format(record) + repr(record.__dict__)
    for secret in (TOKEN, REVIEW_TOKEN, "wrong-synthetic-decision-token"):
        assert secret not in observed


@pytest.mark.parametrize("body", [
    {"decision": "approve"},
    {"decision": "approve", "reviewed_patch_sha256": "synthetic-private-digest"},
    {"decision": "approve", "reviewed_patch_sha256": "B" * 64},
    {"decision": "approve", "reviewed_patch_sha256": 123},
    {"decision": "reject", "reviewed_patch_sha256": DIGEST},
    {"decision": "reject", "workspace_id": "synthetic-private-workspace"},
    {"decision": "reject", "tool_call_id": "synthetic-private-tool-id"},
    {"decision": "reject", "reference": "synthetic-private-reference"},
    {"decision": "reject", "context": "synthetic-private-context"},
    {"decision": "synthetic-private-operation"},
    {"decision": False},
    None,
], ids=[
    "approve-missing", "malformed-digest", "uppercase-digest", "numeric-digest", "reject-digest", "workspace-field",
    "tool-id-field", "reference-field", "context-field", "unknown-variant", "non-string", "null",
])
def test_decision_rejects_invalid_body_before_lookup(review_binding, monkeypatch, http_logs, body):
    lookup = AsyncMock()
    monkeypatch.setattr(ApprovalRepository, "read_for_review", lookup)
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        response = client.post(decision_url(uuid4(), uuid4()), headers=REVIEW_AUTH, json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    lookup.assert_not_awaited()
    [log] = http_logs
    observed = response.text + JsonFormatter().format(log) + repr(log.__dict__)
    assert "synthetic-private" not in observed and DIGEST not in observed and REVIEW_TOKEN not in observed


@pytest.mark.parametrize("invalid", ["run", "approval"])
def test_decision_rejects_invalid_id_before_lookup(review_binding, monkeypatch, invalid):
    lookup = AsyncMock()
    monkeypatch.setattr(ApprovalRepository, "read_for_review", lookup)
    run_id = "private-invalid-run" if invalid == "run" else uuid4()
    approval_id = "private-invalid-approval" if invalid == "approval" else uuid4()
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        response = client.post(decision_url(run_id, approval_id), headers=REVIEW_AUTH, json={"decision": "reject"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "private-invalid" not in response.text
    lookup.assert_not_awaited()


@pytest.mark.parametrize("mode", ["approve", "reject"])
def test_decision_dispatches_exact_scope_and_returns_only_recorded_outcome(review_binding, monkeypatch, mode):
    run_id, approval_id = uuid4(), uuid4()
    row = _record(run_id, approval_id, review_binding.workspace_id)
    row["request_events"] = [{"private": "synthetic-internal-evidence"}]
    read = AsyncMock(return_value=row)
    monkeypatch.setattr(ApprovalRepository, "read_for_review", read)
    port = AsyncMock()
    app = create_app(session_factory=_session, approval_access_binding=review_binding)
    app.dependency_overrides[get_run_coordinator] = lambda: SimpleNamespace(decide_approval=port)
    body = {"decision": mode}
    if mode == "approve":
        body["reviewed_patch_sha256"] = DIGEST
    with TestClient(app) as client:
        response = client.post(decision_url(run_id, approval_id), headers=REVIEW_AUTH, json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "approval_id": str(approval_id), "run_id": str(run_id),
        "decision": "approved" if mode == "approve" else "rejected", "recorded": True,
    }
    read.assert_awaited_once_with(approval_id, run_id, review_binding.workspace_id, request_event_type="approval_requested")
    target, evidence, decision = port.await_args.args
    assert (target.approval_id, target.run_id, target.workspace_id, target.tool_call_id, target.tool_name) == (
        approval_id, run_id, review_binding.workspace_id, row["tool_call_id"], "apply_patch",
    )
    assert evidence is row["request_events"] and decision is ApprovalDecision(mode)
    assert port.await_args.kwargs == {"reviewed_patch_sha256": DIGEST if mode == "approve" else None}


def test_decision_missing_runtime_returns_503_only_for_known_pending_approval(review_binding, monkeypatch):
    run_id, approval_id = uuid4(), uuid4()
    row = _record(run_id, approval_id, review_binding.workspace_id)
    monkeypatch.setattr(ApprovalRepository, "read_for_review", AsyncMock(side_effect=[row, None]))
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        known = client.post(decision_url(run_id, approval_id), headers=REVIEW_AUTH, json={"decision": "reject"})
        unknown = client.post(decision_url(run_id, uuid4()), headers=REVIEW_AUTH, json={"decision": "reject"})
    assert known.status_code == 503 and known.json()["error"]["code"] == "APPROVAL_DECISION_UNAVAILABLE"
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


@pytest.mark.parametrize("failure,expected_status,code", [
    (ApprovalDecisionConflictError, 409, "APPROVAL_DECISION_CONFLICT"),
    (ApprovalReviewIntegrityError, 500, "APPROVAL_DECISION_FAILED"),
    (RuntimeError, 500, "APPROVAL_DECISION_FAILED"),
], ids=["conflict", "integrity", "unexpected"])
def test_decision_sanitizes_internal_errors(review_binding, monkeypatch, http_logs, failure, expected_status, code):
    private = f"synthetic-private-error /host/private {DIGEST}"
    monkeypatch.setattr(ApprovalDecisionService, "decide", AsyncMock(side_effect=failure(private)))
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        response = client.post(decision_url(uuid4(), uuid4()), headers=REVIEW_AUTH,
                               json={"decision": "approve", "reviewed_patch_sha256": DIGEST})
    assert response.status_code == expected_status and response.json()["error"]["code"] == code
    [log] = http_logs
    observed = response.text + JsonFormatter().format(log) + repr(log.__dict__)
    for marker in ("synthetic-private-error", "/host/private", DIGEST, REVIEW_TOKEN):
        assert marker not in observed


def test_decision_cancellation_is_not_translated_to_a_response(review_binding, monkeypatch):
    async def check():
        from fastapi import Response

        from app.api.approvals import post_approval_decision
        from app.schemas.approval import RejectApprovalRequest

        monkeypatch.setattr(ApprovalDecisionService, "decide", AsyncMock(side_effect=asyncio.CancelledError))
        response = Response()
        with pytest.raises(asyncio.CancelledError):
            await post_approval_decision(
                uuid4(), uuid4(), response, review_binding.workspace_id, object(), None,
                RejectApprovalRequest(decision="reject"),
            )
        assert "cache-control" not in response.headers

    asyncio.run(check())


def test_decision_openapi_matches_strict_variants_and_privileged_capability():
    contract = create_app().openapi()
    [operation] = contract["paths"][DECISION_PATH].values()
    assert operation["security"] == [{"LocalApprovalReviewCapability": []}]
    assert set(operation["responses"]) == {"200", "403", "404", "409", "422", "500", "503"}
    schemas = contract["components"]["schemas"]
    request = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request["discriminator"]["propertyName"] == "decision"
    assert set(request["discriminator"]["mapping"]) == {"approve", "reject"}
    assert schemas["ApproveApprovalRequest"]["required"] == ["decision", "reviewed_patch_sha256"]
    assert schemas["RejectApprovalRequest"]["required"] == ["decision"]
    assert not schemas["ApproveApprovalRequest"]["additionalProperties"]
    assert not schemas["RejectApprovalRequest"]["additionalProperties"]
    assert set(schemas["ApprovalDecisionResponse"]["properties"]) == {"approval_id", "run_id", "decision", "recorded"}
    assert set(schemas["ApprovalDecisionResponse"]["required"]) == {"approval_id", "run_id", "decision", "recorded"}
    assert schemas["ApprovalDecisionResponse"]["properties"]["recorded"]["const"] is True
    assert operation["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert TOKEN not in repr(contract) and REVIEW_TOKEN not in repr(contract)
