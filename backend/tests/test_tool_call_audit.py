import json
from uuid import UUID

import pytest

from app.domain.execution import ToolCallStatus as Status
from app.services.tool_call_audit import ToolCallAudit, ToolCallErrorCode as Code, encode_tool_call_event


CALL_ID = UUID(int=7)


@pytest.mark.parametrize(
    ("status", "code", "duration", "event", "extra"),
    [
        (Status.PROPOSED, None, None, "tool_call_proposed", {}),
        (Status.RUNNING, None, None, "tool_call_started", {}),
        (Status.SUCCEEDED, None, 0, "tool_call_finished", {"duration_ms": 0}),
        (Status.DENIED, Code.PERMISSION_DENIED, None, "tool_call_finished", {"error_code": "permission_denied"}),
        (Status.FAILED, Code.INVALID_INPUT, None, "tool_call_finished", {"error_code": "invalid_input"}),
        (Status.FAILED, Code.TOOL_TIMEOUT, 2, "tool_call_finished", {"error_code": "tool_timeout", "duration_ms": 2}),
        (Status.FAILED, Code.CANCELLED, None, "tool_call_finished", {"error_code": "cancelled"}),
    ],
)
def test_tool_call_audit_exact_versioned_payload(status, code, duration, event, extra):
    audit = ToolCallAudit(CALL_ID, "read_file", 1, status, code, duration)
    event_type, payload = encode_tool_call_event(audit)
    assert event_type == event
    assert json.loads(json.dumps(payload)) == {
        "schema_version": 1, "tool_call_id": str(CALL_ID), "tool_name": "read_file",
        "tool_version": 1, "status": status.value, **extra,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"tool_call_id": str(CALL_ID)},
        {"tool_name": "/tmp/synthetic-sentinel"},
        {"tool_name": "read_file\n"},
        {"tool_name": "x" * 65},
        {"tool_version": True},
        {"tool_version": "1"},
        {"tool_version": 0},
        {"status": "proposed"},
        {"error_code": "cancelled", "status": Status.FAILED},
        {"error_code": "synthetic exception payload", "status": Status.FAILED},
        {"error_code": Code.CANCELLED, "status": Status.PROPOSED},
        {"error_code": Code.INTERNAL_ERROR, "status": Status.RUNNING},
        {"error_code": Code.INTERNAL_ERROR, "status": Status.SUCCEEDED},
        {"status": Status.FAILED},
        {"error_code": Code.PERMISSION_DENIED, "status": Status.FAILED},
        {"error_code": Code.INVALID_INPUT, "status": Status.DENIED},
        {"status": Status.DENIED},
        {"status": Status.DENIED, "error_code": Code.EXECUTION_BLOCKED, "duration_ms": 0},
        {"status": Status.RUNNING, "duration_ms": 0},
        {"status": Status.SUCCEEDED, "duration_ms": True},
        {"status": Status.SUCCEEDED, "duration_ms": -1},
        {"status": Status.SUCCEEDED, "duration_ms": 1.0},
    ],
)
def test_tool_call_audit_rejects_invalid_metadata(override):
    fields = dict(tool_call_id=CALL_ID, tool_name="read_file", tool_version=1, status=Status.PROPOSED)
    with pytest.raises(ValueError, match="^invalid_"):
        ToolCallAudit(**(fields | override))


@pytest.mark.parametrize("field", ["arguments", "path", "content", "query", "diff", "exception"])
def test_tool_call_audit_does_not_accept_payload_fields(field):
    with pytest.raises(TypeError):
        ToolCallAudit(CALL_ID, "read_file", 1, Status.PROPOSED, **{field: "synthetic-sentinel"})
