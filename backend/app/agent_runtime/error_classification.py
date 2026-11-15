from langgraph.errors import GraphRecursionError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from app.agent_runtime.agent_graph import AgentGraphError
from app.agent_runtime.finalization import FinalizationError
from app.agent_runtime.structured_output import StructuredOutputError
from app.services.agent_failure import AgentFailureCode


_BUDGET_REASONS = (
    ("model_budget_exceeded",),
    ("tool_budget_exceeded",),
    ("finalization_budget_exceeded",),
)


def classify_runtime_error(error: Exception) -> AgentFailureCode:
    if isinstance(error, APITimeoutError):
        return AgentFailureCode.PROVIDER_TIMEOUT
    if isinstance(error, APIConnectionError):
        return AgentFailureCode.PROVIDER_CONNECTION
    if isinstance(error, APIStatusError):
        return _classify_status_error(error)
    if isinstance(error, APIError):
        return AgentFailureCode.PROVIDER_ERROR
    if isinstance(error, GraphRecursionError):
        return AgentFailureCode.GRAPH_BUDGET_EXCEEDED
    if isinstance(error, AgentGraphError):
        if error.args in _BUDGET_REASONS:
            return AgentFailureCode.GRAPH_BUDGET_EXCEEDED
        return AgentFailureCode.GRAPH_EXECUTION_FAILED
    if isinstance(error, (StructuredOutputError, FinalizationError)):
        return AgentFailureCode.STRUCTURED_OUTPUT_INVALID
    if isinstance(error, TimeoutError):
        return AgentFailureCode.RUNNER_TIMEOUT
    return AgentFailureCode.EXECUTION_FAILED


def _classify_status_error(error: APIStatusError) -> AgentFailureCode:
    status = error.status_code
    if status in (401, 403):
        return AgentFailureCode.PROVIDER_AUTH
    if status == 408:
        return AgentFailureCode.PROVIDER_TIMEOUT
    if status == 429:
        if error.code == "insufficient_quota":
            return AgentFailureCode.PROVIDER_QUOTA_EXHAUSTED
        if error.code == "rate_limit_exceeded":
            return AgentFailureCode.PROVIDER_RATE_LIMITED
        return AgentFailureCode.PROVIDER_RATE_LIMIT_UNKNOWN
    if 500 <= status <= 599:
        return AgentFailureCode.PROVIDER_UNAVAILABLE
    if status in (400, 404, 422):
        return AgentFailureCode.PROVIDER_REQUEST_REJECTED
    return AgentFailureCode.PROVIDER_ERROR
