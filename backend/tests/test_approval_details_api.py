from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sqlalchemy.exc import SQLAlchemyError
from test_approvals_api import AUTH, TOKEN, binding, http_logs

from app.api.agent_runs import get_run_coordinator
from app.core.logging import JsonFormatter
from app.main import create_app
from app.repositories.approvals import ApprovalRepository
from app.services.approval_preview import project_patch_preview
from app.tools.repository.patch import parse_patch


def test_approval_detail_is_fail_closed_without_binding():
    def no_session():
        raise AssertionError("Unconfigured approval detail must not open a session")

    with TestClient(create_app(session_factory=no_session)) as client:
        response = client.get(f"/agent-runs/{uuid4()}/approvals/{uuid4()}")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "APPROVAL_API_UNAVAILABLE"


UNAVAILABLE = {"state": "unavailable", "content_included": False, "files": []}
DETAIL_PATH = "/agent-runs/{run_id}/approvals/{approval_id}"


def detail_url(run_id, approval_id):
    return DETAIL_PATH.format(run_id=run_id, approval_id=approval_id)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer synthetic-invalid-detail-token"}],
                         ids=["missing", "invalid"])
def test_approval_detail_denies_capability_before_session(binding, http_logs, headers):
    session_factory = Mock(side_effect=AssertionError("Denied detail must not open a session"))
    app = create_app(session_factory=session_factory, approval_access_binding=binding)
    with TestClient(app) as client:
        response = client.get(detail_url(uuid4(), uuid4()), headers=headers)

    assert response.status_code == 403
    assert response.json() == {"error": {
        "code": "APPROVAL_ACCESS_DENIED", "message": "Approval access denied", "details": [],
    }}
    session_factory.assert_not_called()
    [record] = http_logs
    assert record.http_fields["route"] == DETAIL_PATH and record.http_fields["status_code"] == 403
    assert "synthetic-invalid-detail-token" not in response.text + JsonFormatter().format(record)
    assert TOKEN not in response.text + JsonFormatter().format(record)


@asynccontextmanager
async def _session():
    yield object()


@pytest.mark.parametrize("invalid", ["run", "approval"])
def test_approval_detail_rejects_malformed_ids(binding, monkeypatch, invalid):
    read = AsyncMock(side_effect=AssertionError("Malformed IDs must not reach SQL"))
    monkeypatch.setattr(ApprovalRepository, "read_for_run", read)
    run_id, approval_id = uuid4(), uuid4()
    if invalid == "run":
        run_id = "synthetic-bad-run-id"
    else:
        approval_id = "synthetic-bad-approval-id"
    with TestClient(create_app(session_factory=_session, approval_access_binding=binding)) as client:
        response = client.get(detail_url(run_id, approval_id), headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "synthetic-bad" not in response.text
    read.assert_not_awaited()


def _record(run_id, approval_id, workspace_id, *, status="pending"):
    return {
        "id": approval_id, "run_id": run_id, "workspace_id": workspace_id,
        "tool_name": "apply_patch", "tool_call_id": uuid4(), "status": status,
        "requested_at": datetime(2026, 1, 1, tzinfo=UTC), "decided_at": None,
        "run_status": "waiting_approval", "tool_call_status": "proposed",
    }


def test_approval_detail_uses_server_workspace_and_exact_ids(binding, monkeypatch):
    run_id, approval_id = uuid4(), uuid4()
    read = AsyncMock(return_value=_record(run_id, approval_id, binding.workspace_id))
    monkeypatch.setattr(ApprovalRepository, "read_for_run", read)
    with TestClient(create_app(session_factory=_session, approval_access_binding=binding)) as client:
        response = client.get(detail_url(run_id, approval_id), params={"workspace_id": str(uuid4())}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "id": str(approval_id), "run_id": str(run_id), "tool_name": "apply_patch", "status": "pending",
        "requested_at": "2026-01-01T00:00:00Z", "decided_at": None, "preview": UNAVAILABLE,
    }
    read.assert_awaited_once_with(approval_id, run_id, binding.workspace_id)


@pytest.mark.parametrize("state", ["sql-failure", "inconsistent-decision", "unsafe-tool-name"])
def test_approval_detail_hides_read_or_projection_errors(binding, monkeypatch, http_logs, state):
    run_id, approval_id = uuid4(), uuid4()
    record = _record(run_id, approval_id, binding.workspace_id)
    sentinel = "synthetic-private-detail /host/workspace/.env"
    if state == "sql-failure":
        read = AsyncMock(side_effect=SQLAlchemyError(sentinel))
    else:
        if state == "inconsistent-decision":
            record["status"] = "approved"
        else:
            record["tool_name"] = sentinel
        read = AsyncMock(return_value=record)
    monkeypatch.setattr(ApprovalRepository, "read_for_run", read)
    with TestClient(create_app(session_factory=_session, approval_access_binding=binding)) as client:
        response = client.get(detail_url(run_id, approval_id), headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "APPROVAL_READ_FAILED", "message": "Approval data is unavailable", "details": [],
    }}
    [log] = http_logs
    for value in (sentinel, TOKEN, str(binding.workspace_id)):
        assert value not in response.text + JsonFormatter().format(log) + repr(log.__dict__)


@pytest.mark.parametrize("status,run_status,tool_status", [
    ("approved", "waiting_approval", "proposed"),
    ("rejected", "waiting_approval", "proposed"),
    ("pending", "cancelled", "proposed"),
    ("pending", "waiting_approval", "failed"),
], ids=["approved", "rejected", "run-not-waiting", "tool-not-proposed"])
def test_approval_detail_skips_live_lookup_if_persisted_state_disqualifies_it(
    binding, monkeypatch, status, run_status, tool_status,
):
    run_id, approval_id = uuid4(), uuid4()
    row = _record(run_id, approval_id, binding.workspace_id, status=status)
    row.update(run_status=run_status, tool_call_status=tool_status)
    if status != "pending":
        row["decided_at"] = row["requested_at"]
    monkeypatch.setattr(ApprovalRepository, "read_for_run", AsyncMock(return_value=row))
    preview = Mock(side_effect=AssertionError("Disqualified approval must not read the runtime"))
    app = create_app(session_factory=_session, approval_access_binding=binding)
    app.dependency_overrides[get_run_coordinator] = lambda: SimpleNamespace(read_approval_preview=preview)
    with TestClient(app) as client:
        response = client.get(detail_url(run_id, approval_id), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == status and response.json()["preview"] == UNAVAILABLE
    preview.assert_not_called()


def test_approval_detail_openapi_exposes_metadata_and_discriminated_preview():
    schema = create_app().openapi()
    [operation] = schema["paths"][DETAIL_PATH].values()
    definitions = schema["components"]["schemas"]
    assert set(operation["responses"]) == {"200", "403", "404", "422", "500", "503"}
    assert all(item["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
               for code, item in operation["responses"].items() if code != "200")
    assert operation["security"] == [{"LocalApprovalCapability": []}]
    assert [(p["in"], p["name"], p["schema"]["format"]) for p in operation["parameters"]] == [
        ("path", "run_id", "uuid"), ("path", "approval_id", "uuid"),
    ]
    properties = definitions["ApprovalDetailResponse"]["properties"]
    assert set(properties) == {"id", "run_id", "tool_name", "status", "requested_at", "decided_at", "preview"}
    assert properties["preview"]["discriminator"]["propertyName"] == "state"
    assert set(properties["preview"]["discriminator"]["mapping"]) == {"live_at_read", "unavailable"}
    assert set(definitions["ApprovalPreviewFile"]["properties"]) == {"path", "added_lines", "removed_lines", "hunk_count"}
    assert definitions["LiveApprovalPreview"]["properties"]["content_included"]["const"] is False


@pytest.mark.parametrize("path", ["../outside.txt", "/absolute.txt", ".env"])
def test_structural_preview_suppresses_parser_rejections(path):
    patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    assert project_patch_preview(patch) is None


def test_structural_preview_fails_closed_on_public_byte_budget():
    path = "漢" * 1400 + ".txt"
    patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    assert parse_patch(patch).paths == (path,)
    assert project_patch_preview(patch) is None
