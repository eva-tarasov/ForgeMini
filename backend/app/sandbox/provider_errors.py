"""Classification of model-provider failures (OpenAI SDK) into stable `AgentFailureCode`s.

Lives in `sandbox/` because the prototype's agent runner needs ONLY this part. Before ADR-003 (cut 1) the function lived
next to the LangGraph graphs, so the sandbox kept the whole graph machinery importable just to tell "401" from "429".
"""
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from app.services.agent_failure import AgentFailureCode


def classify_provider_error(error: Exception) -> AgentFailureCode:
    """Map an exception from a model call to a failure code. Order matters: the narrow SDK subclasses come first."""
    if isinstance(error, APITimeoutError):
        return AgentFailureCode.PROVIDER_TIMEOUT
    if isinstance(error, APIConnectionError):
        return AgentFailureCode.PROVIDER_CONNECTION
    if isinstance(error, APIStatusError):
        return _classify_status_error(error)
    if isinstance(error, APIError):
        return AgentFailureCode.PROVIDER_ERROR
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
        # one status, three different situations: out of money, too fast, or the provider does not say
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
