import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from test_approval_details_api import _record, detail_url
from test_approvals_api import AUTH, TOKEN, http_logs

from app.core.logging import JsonFormatter
from app.main import create_app
from app.repositories.approvals import ApprovalRepository
from app.schemas.approval import ApprovalPatchReview
from app.services.approval_contracts import ApprovalReference, patch_fingerprint
from app.services.approval_preview import ApprovalPreviewTarget, ApprovalReviewIntegrityError
from app.services.approved_repository_agent_runner import ApprovedRepositoryAgentRunner
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.tool_calls import ToolCallTicket


def test_exact_review_is_unavailable_by_default():
    def no_session():
        raise AssertionError("Disabled review must not open a database session")

    with TestClient(create_app(session_factory=no_session)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/approvals/{uuid4()}/review")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_REVIEW_UNAVAILABLE"


REVIEW_TOKEN = "synthetic-dedicated-approval-review-capability"
REVIEW_AUTH = {"Authorization": f"Bearer {REVIEW_TOKEN}"}
REVIEW_PATH = "/agent-runs/{run_id}/approvals/{approval_id}/review"


def review_url(run_id, approval_id):
    return REVIEW_PATH.format(run_id=run_id, approval_id=approval_id)


@pytest.fixture
def review_binding():
    return LocalApprovalAccessBinding(uuid4(), SecretStr(TOKEN), review_token=SecretStr(REVIEW_TOKEN))


def test_exact_review_is_disabled_with_only_read_capability():
    session = Mock(side_effect=AssertionError("Disabled review must not open a session"))
    binding = LocalApprovalAccessBinding(uuid4(), SecretStr(TOKEN))
    with TestClient(create_app(session_factory=session, approval_access_binding=binding)) as client:
        response = client.get(review_url(uuid4(), uuid4()), headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_REVIEW_UNAVAILABLE"
    session.assert_not_called()


@pytest.mark.parametrize("review_token", [
    "", "x" * 31, "x" * 513, "x" * 31 + "!", TOKEN,
], ids=["empty", "short", "long", "invalid-character", "same-as-read"])
def test_review_token_configuration_rejects_invalid_or_reused_secret(review_token):
    with pytest.raises(ValueError) as error:
        LocalApprovalAccessBinding(uuid4(), SecretStr(TOKEN), review_token=SecretStr(review_token))
    assert error.value.args == (
        "Local approval capabilities must use distinct tokens"
        if review_token == TOKEN else "Invalid local approval access token",
    )


def test_review_capabilities_are_separate_and_data_is_redacted_in_repr(review_binding):
    assert review_binding.accepts(TOKEN) and not review_binding.accepts(REVIEW_TOKEN)
    assert review_binding.accepts_review(REVIEW_TOKEN) and not review_binding.accepts_review(TOKEN)
    assert TOKEN not in repr(review_binding) and REVIEW_TOKEN not in repr(review_binding)
    assert "token=" not in repr(review_binding)
    patch = "synthetic-private-raw-patch"
    review = ApprovalPatchReview(approval_id=uuid4(), run_id=uuid4(), patch_sha256="a" * 64, patch=patch)
    assert patch not in repr(review)
    assert review.model_dump()["patch"] == patch


@pytest.mark.parametrize("headers", [
    {},
    AUTH,
    {"Authorization": "Bearer synthetic-wrong-review-token"},
    [("Authorization", f"Bearer {REVIEW_TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
], ids=["missing", "read-only", "wrong", "duplicate"])
def test_review_access_is_rejected_before_sql(review_binding, http_logs, headers):
    session = Mock(side_effect=AssertionError("Denied review must not open a session"))
    with TestClient(create_app(session_factory=session, approval_access_binding=review_binding)) as client:
        response = client.get(review_url(uuid4(), uuid4()), headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": {
        "code": "APPROVAL_ACCESS_DENIED", "message": "Approval access denied", "details": [],
    }}
    session.assert_not_called()
    [record] = http_logs
    assert record.http_fields["route"] == REVIEW_PATH and record.http_fields["status_code"] == 403
    observed = response.text + JsonFormatter().format(record) + repr(record.__dict__)
    for marker in (TOKEN, REVIEW_TOKEN, "synthetic-wrong-review-token"):
        assert marker not in observed


def test_review_token_does_not_change_details_capability(review_binding):
    session = Mock(side_effect=AssertionError("Review token must not open a session for details"))
    with TestClient(create_app(session_factory=session, approval_access_binding=review_binding)) as client:
        response = client.get(detail_url(uuid4(), uuid4()), headers=REVIEW_AUTH)
    assert response.status_code == 403
    session.assert_not_called()


@asynccontextmanager
async def _session():
    yield object()


@pytest.mark.parametrize("bad_field", ["run", "approval"])
def test_review_sanitizes_malformed_identifiers(review_binding, monkeypatch, bad_field):
    read = AsyncMock()
    monkeypatch.setattr(ApprovalRepository, "read_for_review", read)
    bad = "synthetic-private-malformed-id"
    run_id = bad if bad_field == "run" else uuid4()
    approval_id = bad if bad_field == "approval" else uuid4()
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        response = client.get(review_url(run_id, approval_id), headers=REVIEW_AUTH)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert bad not in response.text
    read.assert_not_awaited()


def test_review_sanitizes_sql_failure(review_binding, monkeypatch, http_logs):
    internal = "synthetic-private-sql-value /host/internal"
    monkeypatch.setattr(ApprovalRepository, "read_for_review", AsyncMock(side_effect=SQLAlchemyError(internal)))
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        response = client.get(review_url(uuid4(), uuid4()), headers=REVIEW_AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "APPROVAL_REVIEW_FAILED", "message": "Approval review failed", "details": [],
    }}
    [record] = http_logs
    assert internal not in response.text + JsonFormatter().format(record) + repr(record.__dict__)


def test_review_without_runtime_is_unavailable_for_known_pending_only(review_binding, monkeypatch):
    run_id, approval_id = uuid4(), uuid4()
    row = _record(run_id, approval_id, review_binding.workspace_id)
    read = AsyncMock(side_effect=[row, None])
    monkeypatch.setattr(ApprovalRepository, "read_for_review", read)
    with TestClient(create_app(session_factory=_session, approval_access_binding=review_binding)) as client:
        pending = client.get(review_url(run_id, approval_id), headers=REVIEW_AUTH, params={"workspace_id": str(uuid4())})
        missing = client.get(review_url(run_id, uuid4()), headers=REVIEW_AUTH)
    assert pending.status_code == 503 and pending.json()["error"]["code"] == "APPROVAL_REVIEW_UNAVAILABLE"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert read.await_args_list[0].args == (approval_id, run_id, review_binding.workspace_id)


def test_review_openapi_uses_dedicated_capability_and_no_store():
    schema = create_app().openapi()
    [operation] = schema["paths"][REVIEW_PATH].values()
    responses = operation["responses"]
    definitions = schema["components"]["schemas"]
    assert set(responses) == {"200", "403", "404", "409", "422", "500", "503"}
    assert responses["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert all(item["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
               for code, item in responses.items() if code != "200")
    assert operation["security"] == [{"LocalApprovalReviewCapability": []}]
    assert schema["components"]["securitySchemes"]["LocalApprovalReviewCapability"] == {
        "type": "http", "scheme": "bearer",
    }
    assert schema["paths"][detail_url("{run_id}", "{approval_id}")]["get"]["security"] == [
        {"LocalApprovalCapability": []},
    ]
    assert set(definitions["ApprovalPatchReview"]["properties"]) == {"approval_id", "run_id", "patch_sha256", "patch"}
    assert TOKEN not in repr(schema) and REVIEW_TOKEN not in repr(schema)


@pytest.mark.parametrize("damage", ["altered-patch", "invalid-patch", "duplicate-request"])
def test_live_review_refuses_internal_integrity_mismatch(monkeypatch, damage):
    async def check():
        original = "--- a/test.txt\n+++ b/test.txt\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        ticket = ToolCallTicket(uuid4(), uuid4(), uuid4(), "apply_patch", 1)
        ref = ApprovalReference(uuid4(), ticket, patch_fingerprint(original), "0" * 64)
        target = ApprovalPreviewTarget(ref.approval_id, ticket.run_id, ticket.workspace_id,
                                       ticket.tool_call_id, ticket.tool_name)
        events = [ref.requested_payload()]
        if damage == "duplicate-request":
            events.append(ref.requested_payload())
        patch = {
            "altered-patch": original.replace("+new", "+synthetic-private-change"),
            "invalid-patch": "synthetic-invalid-patch",
            "duplicate-request": original,
        }[damage]
        operation = SimpleNamespace(reference=ref, patch=patch, deadline=asyncio.get_running_loop().time() + 5)
        monkeypatch.setattr(ApprovedRepositoryAgentRunner, "_live_operation", Mock(return_value=operation))
        runner = ApprovedRepositoryAgentRunner.__new__(ApprovedRepositoryAgentRunner)
        with pytest.raises(ApprovalReviewIntegrityError) as error:
            runner.read_approval_review(target, events)
        assert patch not in str(error.value) and ref.patch_sha256 not in str(error.value)

    asyncio.run(check())
