import json
from dataclasses import FrozenInstanceError

import pytest

from app.services.agent_failure import (
    AgentFailure,
    AgentFailureCode,
    AgentFailureContractError,
    RetryDisposition,
    classify_execution_error,
    decode_agent_failure,
    encode_agent_failure,
)
from app.services.agent_runner import AgentRunnerError


POLICY = {
    "execution_deadline_exceeded": "review_required",
    "runner_timeout": "review_required",
    "provider_timeout": "transient_candidate",
    "provider_connection": "transient_candidate",
    "provider_rate_limited": "transient_candidate",
    "provider_quota_exhausted": "not_retryable",
    "provider_rate_limit_unknown": "review_required",
    "provider_auth": "not_retryable",
    "provider_request_rejected": "not_retryable",
    "provider_unavailable": "transient_candidate",
    "provider_error": "review_required",
    "graph_budget_exceeded": "review_required",
    "graph_execution_failed": "review_required",
    "structured_output_invalid": "review_required",
    "invalid_runner_result": "review_required",
    "sandbox_error": "review_required",
    "workspace_not_configured": "review_required",
    "execution_failed": "review_required",
}


def test_failure_contract_has_exact_codes_and_dispositions():
    assert {code.value for code in AgentFailureCode} == POLICY.keys()
    assert {disposition.value for disposition in RetryDisposition} == set(POLICY.values())


@pytest.mark.parametrize("code,disposition", POLICY.items())
def test_failure_round_trip_and_policy(code, disposition):
    failure = classify_execution_error(AgentRunnerError(code))
    assert failure == AgentFailure(AgentFailureCode(code))
    assert failure.retry_disposition is RetryDisposition(disposition)
    payload = encode_agent_failure(failure)
    assert payload == {"schema_version": 1, "code": code}
    assert type(payload["code"]) is str
    assert decode_agent_failure(json.loads(json.dumps(payload))) == failure


def test_failure_is_frozen_and_slotted():
    failure = AgentFailure(AgentFailureCode.EXECUTION_FAILED)
    with pytest.raises(FrozenInstanceError):
        failure.code = AgentFailureCode.RUNNER_TIMEOUT
    assert not hasattr(failure, "__dict__")


@pytest.mark.parametrize("failure", [None, {"code": "execution_failed"}, AgentFailure("execution_failed")])
def test_encode_rejects_invalid_failure(failure):
    with pytest.raises(AgentFailureContractError, match="^invalid_agent_failure$") as exc:
        encode_agent_failure(failure)
    assert exc.value.reason == "invalid_agent_failure"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"schema_version": 1}, {"code": "execution_failed"},
    {"schema_version": 1, "code": "execution_failed", "retry_disposition": "review_required"},
    {"schema_version": 1, "code": "synthetic private detail"},
    {"schema_version": 1, "code": 7},
    {"schema_version": True, "code": "execution_failed"},
    {"schema_version": 1.0, "code": "execution_failed"},
    {"schema_version": "1", "code": "execution_failed"},
])
def test_decode_rejects_invalid_failure_payload(payload):
    with pytest.raises(AgentFailureContractError, match="^invalid_stored_failure$") as exc:
        decode_agent_failure(payload)
    assert exc.value.reason == "invalid_stored_failure"


def test_decode_rejects_unsupported_failure_version():
    with pytest.raises(AgentFailureContractError, match="^unsupported_failure_version$") as exc:
        decode_agent_failure({"schema_version": 2, "code": "execution_failed"})
    assert exc.value.reason == "unsupported_failure_version"


@pytest.mark.parametrize("error,code", [
    (AgentRunnerError("synthetic private detail"), "execution_failed"),
    (AgentRunnerError(None), "execution_failed"),
    (TimeoutError("synthetic private detail"), "runner_timeout"),
    (RuntimeError("provider_timeout"), "execution_failed"),
])
def test_classification_only_persists_allowlisted_codes(error, code):
    assert encode_agent_failure(classify_execution_error(error)) == {"schema_version": 1, "code": code}


def test_application_classification_does_not_follow_exception_chain():
    error = AgentRunnerError("execution_failed")
    error.__cause__ = TimeoutError("synthetic private detail")
    assert classify_execution_error(error) == AgentFailure(AgentFailureCode.EXECUTION_FAILED)
