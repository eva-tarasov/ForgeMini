from enum import StrEnum


class ToolErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_TOOL = "unknown_tool"
    VERSION_MISMATCH = "version_mismatch"
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    EXECUTION_BLOCKED = "execution_blocked"
    INVALID_HANDLER = "invalid_handler"
    TOOL_TIMEOUT = "tool_timeout"
    HANDLER_FAILED = "handler_failed"
    INVALID_OUTPUT = "invalid_output"
    INTERNAL_ERROR = "internal_error"
    APPROVAL_INVALID = "approval_invalid"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_SOURCE_CHANGED = "approval_source_changed"
    PARTIAL_SIDE_EFFECT = "patch_partial_apply"


class PartialSideEffectError(RuntimeError):
    """A trusted handler observed a partially completed side effect."""


class ToolExecutionError(RuntimeError):
    """Execution failure with a stable, payload-free message."""

    def __init__(self, code: ToolErrorCode) -> None:
        self.code = code
        super().__init__(code.value)
