from dataclasses import dataclass
from enum import StrEnum

from app.services.agent_runner import AgentRunnerError


FAILURE_SCHEMA_VERSION = 1
AGENT_EXECUTION_FAILED_EVENT = "agent_execution_failed"


class AgentFailureCode(StrEnum):
    EXECUTION_DEADLINE_EXCEEDED = "execution_deadline_exceeded"
    RUNNER_TIMEOUT = "runner_timeout"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION = "provider_connection"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
    PROVIDER_RATE_LIMIT_UNKNOWN = "provider_rate_limit_unknown"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_REQUEST_REJECTED = "provider_request_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    GRAPH_BUDGET_EXCEEDED = "graph_budget_exceeded"
    GRAPH_EXECUTION_FAILED = "graph_execution_failed"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    INVALID_RUNNER_RESULT = "invalid_runner_result"
    SANDBOX_ERROR = "sandbox_error"
    WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"
    SETUP_FAILED = "setup_failed"                  # the user's --setup command failed (ADR-004)
    EXECUTION_FAILED = "execution_failed"


class RetryDisposition(StrEnum):
    TRANSIENT_CANDIDATE = "transient_candidate"
    REVIEW_REQUIRED = "review_required"
    NOT_RETRYABLE = "not_retryable"


_TRANSIENT_CODES = frozenset({
    AgentFailureCode.PROVIDER_TIMEOUT,
    AgentFailureCode.PROVIDER_CONNECTION,
    AgentFailureCode.PROVIDER_RATE_LIMITED,
    AgentFailureCode.PROVIDER_UNAVAILABLE,
})
_NON_RETRYABLE_CODES = frozenset({
    AgentFailureCode.PROVIDER_QUOTA_EXHAUSTED,
    AgentFailureCode.PROVIDER_AUTH,
    AgentFailureCode.PROVIDER_REQUEST_REJECTED,
})


@dataclass(frozen=True, slots=True)
class AgentFailure:
    code: AgentFailureCode

    @property
    def retry_disposition(self) -> RetryDisposition:
        if self.code in _TRANSIENT_CODES:
            return RetryDisposition.TRANSIENT_CANDIDATE
        if self.code in _NON_RETRYABLE_CODES:
            return RetryDisposition.NOT_RETRYABLE
        return RetryDisposition.REVIEW_REQUIRED


class AgentFailureContractError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_execution_error(error: Exception) -> AgentFailure:
    if isinstance(error, AgentRunnerError):
        if isinstance(error.reason, str):
            try:
                return AgentFailure(AgentFailureCode(error.reason))
            except ValueError:
                pass
    elif isinstance(error, TimeoutError):
        return AgentFailure(AgentFailureCode.RUNNER_TIMEOUT)
    return AgentFailure(AgentFailureCode.EXECUTION_FAILED)


def encode_agent_failure(failure: AgentFailure) -> dict:
    if not isinstance(failure, AgentFailure) or not isinstance(failure.code, AgentFailureCode):
        raise AgentFailureContractError("invalid_agent_failure")
    return {"schema_version": FAILURE_SCHEMA_VERSION, "code": failure.code.value}


def decode_agent_failure(payload: object) -> AgentFailure:
    if not isinstance(payload, dict) or payload.keys() != {"schema_version", "code"}:
        raise AgentFailureContractError("invalid_stored_failure")
    version = payload["schema_version"]
    if type(version) is not int:
        raise AgentFailureContractError("invalid_stored_failure")
    if version != FAILURE_SCHEMA_VERSION:
        raise AgentFailureContractError("unsupported_failure_version")
    code = payload["code"]
    if not isinstance(code, str):
        raise AgentFailureContractError("invalid_stored_failure")
    try:
        return AgentFailure(AgentFailureCode(code))
    except ValueError:
        raise AgentFailureContractError("invalid_stored_failure") from None
