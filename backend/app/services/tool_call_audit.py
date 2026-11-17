"""Encode only registered identity and lifecycle metadata for ToolCall events."""

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.execution import ToolCallStatus
from app.tools.errors import ToolErrorCode


class ToolCallErrorCode(StrEnum):
    INVALID_REQUEST = ToolErrorCode.INVALID_REQUEST.value
    UNKNOWN_TOOL = ToolErrorCode.UNKNOWN_TOOL.value
    VERSION_MISMATCH = ToolErrorCode.VERSION_MISMATCH.value
    INVALID_INPUT = ToolErrorCode.INVALID_INPUT.value
    PERMISSION_DENIED = ToolErrorCode.PERMISSION_DENIED.value
    EXECUTION_BLOCKED = ToolErrorCode.EXECUTION_BLOCKED.value
    INVALID_HANDLER = ToolErrorCode.INVALID_HANDLER.value
    TOOL_TIMEOUT = ToolErrorCode.TOOL_TIMEOUT.value
    HANDLER_FAILED = ToolErrorCode.HANDLER_FAILED.value
    INVALID_OUTPUT = ToolErrorCode.INVALID_OUTPUT.value
    INTERNAL_ERROR = ToolErrorCode.INTERNAL_ERROR.value
    APPROVAL_INVALID = ToolErrorCode.APPROVAL_INVALID.value
    APPROVAL_REJECTED = ToolErrorCode.APPROVAL_REJECTED.value
    APPROVAL_TIMEOUT = ToolErrorCode.APPROVAL_TIMEOUT.value
    APPROVAL_SOURCE_CHANGED = ToolErrorCode.APPROVAL_SOURCE_CHANGED.value
    PARTIAL_SIDE_EFFECT = ToolErrorCode.PARTIAL_SIDE_EFFECT.value
    CANCELLED = "cancelled"
    RUN_NOT_RUNNING = "run_not_running"
    AUDIT_PERSISTENCE_FAILED = "audit_persistence_failed"


class ToolCallAuditError(RuntimeError):
    """Stable application error for an audit admission or persistence failure."""

    def __init__(self, code: ToolCallErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


_DENIALS = frozenset({
    ToolCallErrorCode.PERMISSION_DENIED, ToolCallErrorCode.EXECUTION_BLOCKED, ToolCallErrorCode.APPROVAL_REJECTED,
})
_EVENT_TYPES = {
    ToolCallStatus.PROPOSED: "tool_call_proposed",
    ToolCallStatus.RUNNING: "tool_call_started",
    ToolCallStatus.SUCCEEDED: "tool_call_finished",
    ToolCallStatus.FAILED: "tool_call_finished",
    ToolCallStatus.DENIED: "tool_call_finished",
}


@dataclass(frozen=True, slots=True)
class ToolCallAudit:
    tool_call_id: UUID
    tool_name: str
    tool_version: int
    status: ToolCallStatus
    error_code: ToolCallErrorCode | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, UUID):
            raise ValueError("invalid_tool_call_id")
        if type(self.tool_name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.tool_name):
            raise ValueError("invalid_tool_name")
        if type(self.tool_version) is not int or self.tool_version < 1:
            raise ValueError("invalid_tool_version")
        if not isinstance(self.status, ToolCallStatus):
            raise ValueError("invalid_tool_call_status")
        if self.error_code is not None and not isinstance(self.error_code, ToolCallErrorCode):
            raise ValueError("invalid_tool_call_error_code")
        if self.status is ToolCallStatus.DENIED:
            valid_error = self.error_code in _DENIALS
        elif self.status is ToolCallStatus.FAILED:
            valid_error = self.error_code is not None and self.error_code not in _DENIALS
        else:
            valid_error = self.error_code is None
        if not valid_error:
            raise ValueError("invalid_tool_call_status_error")
        if self.duration_ms is not None and (
            self.status not in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED)
            or type(self.duration_ms) is not int
            or self.duration_ms < 0
        ):
            raise ValueError("invalid_tool_call_duration")


def encode_tool_call_event(audit: ToolCallAudit) -> tuple[str, dict[str, str | int]]:
    """Omit error_code for nonfailures and duration_ms before start or when unknown."""
    payload: dict[str, str | int] = {
        "schema_version": 1,
        "tool_call_id": str(audit.tool_call_id),
        "tool_name": audit.tool_name,
        "tool_version": audit.tool_version,
        "status": audit.status.value,
    }
    if audit.error_code is not None:
        payload["error_code"] = audit.error_code.value
    if audit.duration_ms is not None:
        payload["duration_ms"] = audit.duration_ms
    return _EVENT_TYPES[audit.status], payload
