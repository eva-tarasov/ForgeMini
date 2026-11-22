"""Provider-error classification used by the prototype's runner. No LangGraph in sight (ADR-003, cut 1)."""
import subprocess
import sys
from pathlib import Path

import httpx
import openai
import pytest

from app.sandbox.provider_errors import classify_provider_error
from app.services.agent_failure import AgentFailureCode

URL = "https://forge-test.invalid/v1/chat/completions"
REQUEST = httpx.Request("POST", URL)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("status,provider_code,expected", [
    (400, None, "provider_request_rejected"),
    (401, None, "provider_auth"),
    (403, None, "provider_auth"),
    (404, None, "provider_request_rejected"),
    (408, None, "provider_timeout"),
    (409, None, "provider_error"),
    (422, None, "provider_request_rejected"),
    (429, "insufficient_quota", "provider_quota_exhausted"),
    (429, "rate_limit_exceeded", "provider_rate_limited"),
    (429, "synthetic unknown", "provider_rate_limit_unknown"),
    (429, None, "provider_rate_limit_unknown"),
    (500, None, "provider_unavailable"),
    (599, None, "provider_unavailable"),
])
def test_status_errors(status, provider_code, expected):
    error = openai.APIStatusError(
        "synthetic error", response=httpx.Response(status, request=REQUEST), body={"code": provider_code},
    )
    assert classify_provider_error(error) is AgentFailureCode(expected)


@pytest.mark.parametrize("error,expected", [
    (openai.APITimeoutError(REQUEST), "provider_timeout"),                 # subclass of APIConnectionError: must win
    (openai.APIConnectionError(request=REQUEST), "provider_connection"),
    (openai.APIError("synthetic error", REQUEST, body=None), "provider_error"),
    (TimeoutError("synthetic error"), "runner_timeout"),
    (RuntimeError("provider_timeout"), "execution_failed"),                # the MESSAGE never decides, only the type
])
def test_exception_types(error, expected):
    assert classify_provider_error(error) is AgentFailureCode(expected)


def test_production_app_does_not_import_langgraph_or_langchain():
    """ADR-003 guard (C2 cut, C4 removal): composing the whole app — HTTP app, bootstrap, sandbox runner — must not
    import LangGraph, the `langchain` meta-package or the deleted M1 runtime. Only langchain_core (message types) and
    langchain_openai (the chat model) are dependencies of the prototype."""
    code = ("import sys, app.main, app.bootstrap, app.sandbox.runner; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('langgraph', 'langchain') "
            "or m.startswith('app.agent_runtime')); "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True)
    assert done.returncode == 0, f"production imports: {done.stdout.strip()} {done.stderr[-300:]}"
