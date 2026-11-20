import asyncio
import ipaddress
import json
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from langgraph.graph.state import CompiledStateGraph
from langsmith import get_tracing_context
from sqlalchemy import select
from test_agent_observability import agent_log_capture
from test_approvals_api import http_logs

from app.agent_runtime import repository_graph
from app.core.logging import JsonFormatter
from app.permissions.policy import PermissionOutcome, PermissionPolicy, PermissionReason
from app.repositories.agent_events import AgentEventRepository
from app.tools.repository.filesystem import RepositoryError, SafeRepositoryReader
from app.tools.repository.schemas import ReadFileInput
from scripts import week4_live_smoke as smoke


@pytest.fixture
def dedicated_env(monkeypatch):
    for name, value in {
        "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "50088",
        "POSTGRES_DB": smoke.TEST_DB, "FORGE_REPOSITORY_AGENT_TEST_DB": smoke.TEST_DB,
    }.items():
        monkeypatch.setenv(name, value)


def test_live_requires_opt_in_before_loading_credentials_or_connecting(monkeypatch):
    monkeypatch.delenv("FORGE_WEEK4_LIVE_SMOKE", raising=False)
    original_get = os.environ.get

    def guarded_get(name, default=None):
        assert name not in {"OPENAI_API_KEY", "FORGE_MODEL_NAME", "POSTGRES_HOST"}
        return original_get(name, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    monkeypatch.setattr(smoke, "create_engine", lambda *_: pytest.fail("missing opt-in reached PostgreSQL"))
    with pytest.raises(smoke.SmokeError, match="^live_opt_in_required$"):
        asyncio.run(smoke.run_smoke(live=True))


@pytest.mark.parametrize(
    ("model", "key", "error"),
    [
        ("synthetic-wrong-model", "synthetic-valid-secret", "unsupported_model"),
        (smoke.MODEL, "synthetic-invalid secret", "api_key_invalid"),
        (smoke.MODEL, "", "api_key_invalid"),
    ],
    ids=["unsupported-model", "malformed-key", "missing-key"],
)
def test_live_configuration_errors_do_not_echo_secrets(monkeypatch, model, key, error):
    monkeypatch.setenv("FORGE_WEEK4_LIVE_SMOKE", "1")
    monkeypatch.setenv("FORGE_MODEL_NAME", model)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(smoke, "create_engine", lambda *_: pytest.fail("invalid live config reached PostgreSQL"))
    with pytest.raises(smoke.SmokeError, match=f"^{error}$"):
        asyncio.run(smoke.run_smoke(live=True))


def test_cli_rejects_unknown_arguments_without_echoing_them(capsys):
    with pytest.raises(SystemExit) as error:
        smoke.main(["--dry-run", "--api-key", "synthetic-argument-secret"])
    assert error.value.code == 2
    assert capsys.readouterr().err == "smoke_refused_or_failed: invalid_cli_arguments\n"


def test_unexpected_local_failure_prints_only_a_fixed_code(monkeypatch, capsys):
    async def fail(_live):
        raise RuntimeError("synthetic-key-at-/Users/private/path")

    monkeypatch.setattr(smoke, "run_smoke", fail)
    assert smoke.main(["--dry-run"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "smoke_refused_or_failed: configuration_or_runtime_error\n"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POSTGRES_HOST", "192.0.2.1"),
        ("POSTGRES_PORT", "5432"),
        ("POSTGRES_DB", "shared"),
        ("FORGE_REPOSITORY_AGENT_TEST_DB", "unbound"),
    ],
    ids=["host", "port", "name", "opt-in"],
)
def test_wrong_database_refused_before_connection(dedicated_env, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(smoke, "create_engine", lambda *_: pytest.fail("unsafe DB reached engine construction"))
    assert smoke.main(["--dry-run"]) == 1
    assert capsys.readouterr().err == "smoke_refused_or_failed: dedicated_db_required\n"


@pytest.mark.parametrize(
    ("identity", "revision"),
    [
        (("other", "170011", "read committed"), "1d972cc34fbe"),
        ((smoke.TEST_DB, "170010", "read committed"), "1d972cc34fbe"),
        ((smoke.TEST_DB, "170011", "repeatable read"), "1d972cc34fbe"),
        ((smoke.TEST_DB, "170011", "read committed"), "older"),
    ],
    ids=["name", "version", "isolation", "revision"],
)
def test_database_identity_mismatch_refused_in_read_only_transaction(identity, revision):
    session = AsyncMock()
    session.execute.side_effect = [
        None, Mock(one=Mock(return_value=identity)), Mock(scalar_one=Mock(return_value=revision)),
    ]

    @asynccontextmanager
    async def begin():
        yield session

    with pytest.raises(smoke.SmokeError, match="^db_preflight_mismatch$"):
        asyncio.run(smoke.verify_db(Mock(begin=begin)))
    assert str(session.execute.await_args_list[0].args[0]) == "SET TRANSACTION READ ONLY"


def test_dry_run_ignores_live_model_and_key(monkeypatch):
    monkeypatch.setenv("FORGE_MODEL_NAME", "synthetic-unapproved-model")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-key")
    monkeypatch.setenv("FORGE_WEEK4_LIVE_SMOKE", "1")
    monkeypatch.setattr(smoke, "token_urlsafe", lambda _: "synthetic-offline-only-key")
    settings = smoke.load_model_settings(live=False)
    assert settings.forge_model_name == smoke.MODEL
    assert settings.openai_api_key.get_secret_value() == "synthetic-offline-only-key"
    assert settings.forge_model_timeout_seconds == 10.0


def test_runner_refuses_write_grant_and_broader_scope(tmp_path):
    binding = smoke.RepositoryFixtureBinding(
        uuid4(), tmp_path, smoke.FixtureBaseline("test", {}), smoke.READ_ONLY, (smoke.PathScope(smoke.FILE),),
    )
    for unsafe in (
        replace(binding, capabilities=smoke.READ_ONLY | {smoke.Capability.REPOSITORY_WRITE}),
        replace(binding, read_scopes=(smoke.PathScope("broader", recursive=True),)),
    ):
        with pytest.raises(smoke.SmokeError, match="^read_only_fixture_required$"):
            smoke.build_runner(None, None, unsafe)


def _research_payload():
    return {
        "model": smoke.MODEL, "reasoning_effort": "none", "max_completion_tokens": smoke.MAX_OUTPUT_TOKENS,
        "parallel_tool_calls": False,
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"max_completion_tokens": 513}, "invalid_model_request_budget"),
        ({"max_tokens": 512}, "invalid_model_request_budget"),
        ({"n": 1}, "invalid_model_request_budget"),
        ({"reasoning_effort": "low"}, "invalid_model_request_budget"),
        ({"tools": [{"type": "function", "function": {"name": "apply_patch"}}]}, "read_only_tools_required"),
        ({"tools": [
            {"type": "function", "function": {"name": name}} for name in ("read_file", "list_files")
        ]}, "read_only_tools_required"),
        ({"parallel_tool_calls": True}, "read_only_tools_required"),
        ({"tools": [], "response_format": {"type": "json_schema"}}, "invalid_structured_output_request"),
        ({"messages": ["x" * smoke.MAX_REQUEST_BYTES]}, "model_request_too_large"),
    ],
    ids=[
        "output-cap", "legacy-cap", "multiple-choice", "reasoning", "write-tool",
        "extra-read-tool", "parallel-tools", "schema", "input",
    ],
)
def test_wire_guard_rejects_unsafe_payload(changes, error):
    transport = smoke.BoundedTransport(
        smoke.OFFLINE_URL, httpx.MockTransport(lambda _: pytest.fail("unsafe request was forwarded")),
    )
    request = httpx.Request(
        "POST", f"{smoke.OFFLINE_URL}/chat/completions", json=_research_payload() | changes,
    )
    with pytest.raises(smoke.SmokeError, match=f"^{error}$"):
        asyncio.run(transport.handle_async_request(request))
    assert transport.calls == 0


def test_wire_guard_refuses_wrong_endpoint_and_fourth_request():
    forwarded = []
    transport = smoke.BoundedTransport(
        smoke.OFFLINE_URL, httpx.MockTransport(lambda request: forwarded.append(request) or httpx.Response(200)),
    )

    async def check():
        bad_url = httpx.Request("POST", f"{smoke.OPENAI_URL}/chat/completions", json=_research_payload())
        with pytest.raises(smoke.SmokeError, match="^unexpected_model_endpoint$"):
            await transport.handle_async_request(bad_url)
        for _ in range(3):
            request = httpx.Request("POST", transport.endpoint, json=_research_payload())
            await transport.handle_async_request(request)
        with pytest.raises(smoke.SmokeError, match="^model_call_budget_exceeded$"):
            await transport.handle_async_request(request)

    asyncio.run(check())
    assert len(forwarded) == transport.calls == 3


@pytest.mark.skipif(
    os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated Week 4 PostgreSQL fixture",
)
@pytest.mark.parametrize("use_tool", [True, False], ids=["read-audited", "no-tool-partial"])
def test_dry_run_uses_only_loopback_postgres_and_sanitizes_output(
    monkeypatch, capsys, agent_log_capture, http_logs, use_tool,
):
    socket_connect = socket.socket.connect
    connections = []
    provider_requests = []
    mock_key = "synthetic-offline-auth-marker"
    read_capability = "synthetic-http-read-capability-marker-123456"
    public_output = []
    ids = []
    cleanup = smoke.cleanup
    original_reply = smoke.offline_reply
    original_response_body = smoke.response_body

    def loopback_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert (ipaddress.ip_address(address[0]), address[1]) == (ipaddress.ip_address("127.0.0.1"), 50088)
            connections.append(address)
        return socket_connect(sock, address)

    def mock_reply(request):
        assert get_tracing_context()["enabled"] is False
        assert request.headers["authorization"] == f"Bearer {mock_key}"
        assert request.extensions["timeout"]["read"] == smoke.REQUEST_TIMEOUT
        provider_requests.append(json.loads(request.content))
        response = original_reply(request)
        if not use_tool and "tools" in provider_requests[-1]:
            payload = response.json()
            payload["choices"][0].update(finish_reason="stop", message={
                "role": "assistant", "content": "No tool was requested in this synthetic response.",
            })
            return httpx.Response(200, json=payload)
        return response

    def capture_response(response, status=200):
        public_output.append(response.text)
        return original_response_body(response, status)

    async def check_cleanup(factory, *row_ids):
        ids.extend(row_ids)
        await cleanup(factory, *row_ids)
        async with factory() as session:
            for model, row_id in zip((smoke.WorkspaceModel, smoke.TaskModel, smoke.AgentRunModel), ids, strict=True):
                assert await session.get(model, row_id) is None

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(smoke.httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("dry-run created a live transport"))
    tokens = iter((mock_key, read_capability))
    monkeypatch.setattr(smoke, "token_urlsafe", lambda _: next(tokens))
    monkeypatch.setattr(smoke, "offline_reply", mock_reply)
    monkeypatch.setattr(smoke, "response_body", capture_response)
    monkeypatch.setattr(smoke, "cleanup", check_cleanup)
    assert smoke.main(["--dry-run"]) == (0 if use_tool else 2)

    requests = 3 if use_tool else 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "outcome": "OFFLINE_PASS" if use_tool else "OFFLINE_PARTIAL_NO_TOOL_CALL",
        "model_requests": requests, "tool_calls": int(use_tool), "verification": "pass",
        "last_model_stage": "finalization",
        "fixture_cleanup": "completed",
        "tool_call_audit": [{"tool_name": "read_file", "status": "succeeded"}] if use_tool else [],
        "usage": {
            "cost_complete": False, "known_cost_usd": None,
            "known_total_tokens": requests * 30, "usage_complete": True,
        },
    }
    assert len(provider_requests) == requests and connections and len(ids) == 3
    for research in provider_requests[:-1]:
        [tool] = research["tools"]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read_file"
        assert tool["function"]["parameters"] == ReadFileInput.model_json_schema()
        assert research["parallel_tool_calls"] is False
    finalization = provider_requests[-1]
    assert "tools" not in finalization
    assert finalization["response_format"]["type"] == "json_schema"
    assert finalization["response_format"]["json_schema"]["strict"] is True
    assert sum(record.http_fields["method"] == "POST" for record in http_logs) == 1
    agent_output = agent_log_capture.getvalue()
    assert "agent_operation_finished" in agent_output
    formatter = JsonFormatter()
    http_output = "".join(formatter.format(record) + repr(record.__dict__) for record in http_logs)
    log_and_http = agent_output + http_output + output.err + "".join(public_output)
    for private in (mock_key, read_capability, smoke.CONTENT.decode().strip(), smoke.GOAL, "Synthetic sample observed"):
        assert private not in log_and_http
    wire = json.dumps(provider_requests)
    assert mock_key not in wire and read_capability not in wire
    assert "forge-week4-smoke-" not in wire and all(str(row_id) not in wire for row_id in ids)


@pytest.mark.skipif(
    os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated Week 4 PostgreSQL fixture",
)
def test_offline_rejects_unadvertised_list_files_before_tool_call(
    monkeypatch, capsys, caplog, agent_log_capture, http_logs,
):
    private = "synthetic-unadvertised-response-marker"
    requests = []
    inspected = []
    offline_reply = smoke.offline_reply
    cleanup = smoke.cleanup

    def substitute(request):
        requests.append(request.url)
        response = offline_reply(request).json()
        message = response["choices"][0]["message"]
        message["content"] = private
        message["tool_calls"][0]["function"] = {
            "name": "list_files", "arguments": json.dumps({"directory": smoke.FILE}),
        }
        return httpx.Response(200, json=response)

    async def check_cleanup(factory, workspace_id, task_id, run_id):
        try:
            async with factory() as session:
                call_id = await session.scalar(select(smoke.ToolCallModel.id).where(
                    smoke.ToolCallModel.agent_run_id == run_id,
                ).limit(1))
                event_id = await session.scalar(select(smoke.AgentEventModel.id).where(
                    smoke.AgentEventModel.agent_run_id == run_id,
                    smoke.AgentEventModel.event_type.in_(("tool_call_proposed", "tool_call_started", "tool_call_finished")),
                ).limit(1))
                inspected.append((call_id, event_id))
        finally:
            await cleanup(factory, workspace_id, task_id, run_id)

    monkeypatch.setattr(smoke, "offline_reply", substitute)
    monkeypatch.setattr(smoke, "cleanup", check_cleanup)
    monkeypatch.setattr(smoke.httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("dry-run created a live transport"))
    verify = AsyncMock(side_effect=AssertionError("rejected tool call reached verification"))
    monkeypatch.setattr(smoke.VerificationService, "verify", verify)

    assert smoke.main(["--dry-run"]) == 1
    output = capsys.readouterr()
    assert output.out, output.err
    report = json.loads(output.out)
    assert report == {
        "outcome": "OFFLINE_FAILED", "failure_code": "graph_execution_failed", "failure_phase": "unknown",
        "model_requests": 1, "last_model_stage": "research", "tool_calls": 0,
        "error_type": "graph_error", "tool_call_audit": [], "verification": None,
        "fixture_cleanup": "completed",
        "usage": {
            "known_total_tokens": 30, "usage_complete": True,
            "known_cost_usd": None, "cost_complete": False,
        },
    }
    assert requests == [httpx.URL(f"{smoke.OFFLINE_URL}/chat/completions")]
    assert inspected == [(None, None)]
    verify.assert_not_called()
    logs = caplog.text + agent_log_capture.getvalue() + repr([record.__dict__ for record in http_logs])
    assert private not in output.out + output.err + logs


@pytest.mark.skipif(
    os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated Week 4 PostgreSQL fixture",
)
@pytest.mark.parametrize(
    ("failure", "expected", "requests"),
    [
        ("400", "provider_request_rejected", 1),
        ("401", "provider_auth", 1),
        ("429-quota", "provider_quota_exhausted", 1),
        ("429-rate", "provider_rate_limited", 1),
        ("429-unknown", "provider_rate_limit_unknown", 1),
        ("307", "provider_error", 1),
        ("timeout", "provider_timeout", 1),
        ("connection", "provider_connection", 1),
        ("local-budget", "model_request_too_large", 0),
        ("invalid-schema", "structured_output_invalid", 3),
        ("length", "structured_output_invalid", 3),
        ("content-filter", "structured_output_invalid", 3),
    ],
)
def test_offline_failure_reports_safe_persisted_code_without_retry(
    monkeypatch, capsys, caplog, agent_log_capture, failure, expected, requests,
):
    calls = []
    private = "synthetic-provider-diagnostic-marker"
    happy_reply = smoke.offline_reply
    cleanup = smoke.cleanup
    cleaned = []

    async def check_cleanup(factory, workspace_id, task_id, run_id):
        await cleanup(factory, workspace_id, task_id, run_id)
        async with factory() as session:
            assert await session.get(smoke.AgentRunModel, run_id) is None
        cleaned.append(run_id)

    def reply(request):
        calls.append(request.url)
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "connection":
            raise httpx.ConnectError(private, request=request)
        if failure == "307":
            return httpx.Response(307, headers={"location": f"{smoke.OPENAI_URL}/chat/completions"})
        if failure[:3].isdigit():
            code = {"429-rate": "rate_limit_exceeded", "429-quota": "insufficient_quota"}.get(failure, private)
            return httpx.Response(
                int(failure[:3]), json={"error": {"message": private, "code": code}},
                headers={"x-should-retry": "true", "retry-after": "0.01"},
            )
        response = happy_reply(request)
        if "response_format" not in json.loads(request.content):
            return response
        payload = response.json()
        choice = payload["choices"][0]
        if failure == "invalid-schema":
            choice["message"]["content"] = json.dumps({"unexpected_field": private})
        else:
            choice["finish_reason"] = failure.replace("-", "_")
        return httpx.Response(200, json=payload)

    if failure == "local-budget":
        monkeypatch.setattr(smoke, "MAX_REQUEST_BYTES", 1)
    monkeypatch.setattr(smoke, "offline_reply", reply)
    monkeypatch.setattr(smoke, "cleanup", check_cleanup)
    monkeypatch.setattr(smoke.httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("dry-run created a live transport"))
    verify = AsyncMock(side_effect=AssertionError("failed execution reached verification"))
    monkeypatch.setattr(smoke.VerificationService, "verify", verify)
    assert smoke.main(["--dry-run"]) == 1
    output = capsys.readouterr()
    assert output.out, output.err
    report = json.loads(output.out)
    expected_error_type = {
        "timeout": "api_timeout",
        "connection": "api_connection_error",
        "local-budget": "smoke_guard",
        "invalid-schema": "validation_error",
        "length": "length_finish_reason",
        "content-filter": "content_filter_finish_reason",
    }.get(failure, "api_status_error")
    assert report == {
        "outcome": "OFFLINE_FAILED", "failure_code": expected, "model_requests": requests,
        "tool_calls": int(requests == 3), "verification": None, "fixture_cleanup": "completed",
        "last_model_stage": {0: "unknown", 1: "research", 3: "finalization"}[requests],
        "failure_phase": "finalization_model" if requests == 3 else "unknown",
        "error_type": expected_error_type,
        "tool_call_audit": [{"tool_name": "read_file", "status": "succeeded"}] if requests == 3 else [],
        "usage": {
            "cost_complete": False, "known_cost_usd": None,
            "known_total_tokens": 60 if requests == 3 else 0, "usage_complete": False,
        },
    }
    assert calls == [httpx.URL(f"{smoke.OFFLINE_URL}/chat/completions")] * requests
    assert len(cleaned) == 1
    verify.assert_not_called()
    assert private not in output.out + output.err + caplog.text + agent_log_capture.getvalue()


@pytest.mark.skipif(
    os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated Week 4 PostgreSQL fixture",
)
@pytest.mark.parametrize(
    ("scenario", "failure", "phase", "error_type", "complete"),
    [
        ("valid", None, None, None, True),
        ("blocked", "preliminary_blocked", "finalization_blocked", "none", True),
        ("invalid-json", "structured_output_invalid", "finalization_model", "validation_error", False),
        ("invalid-fields", "structured_output_invalid", "finalization_model", "validation_error", False),
        ("parser-exception", "structured_output_invalid", "finalization_parser", "value_error", True),
        ("refusal", "structured_output_invalid", "finalization_parser", "model_refusal", True),
        ("blank-summary", "structured_output_invalid", "finalization_validation", "finalization_error", True),
        ("invalid-graph-result", "graph_execution_failed", "graph_result", "graph_error", True),
        ("unexpected-runtime", "execution_failed", "unknown", "runtime_error", True),
    ],
    ids=[
        "valid", "blocked", "invalid-json", "invalid-fields", "parser-exception", "refusal",
        "blank-summary", "invalid-graph-result", "unexpected-runtime",
    ],
)
def test_provider_shaped_finalization_preserves_safe_outcome_and_usage(
    monkeypatch, capsys, caplog, agent_log_capture, http_logs, scenario, failure, phase, error_type, complete,
):
    private = "synthetic-private-provider-finalization"
    requests = []
    persisted = []
    offline_reply = smoke.offline_reply
    cleanup = smoke.cleanup
    graph_invoke = CompiledStateGraph.ainvoke
    model_key = "synthetic-finalization-model-key"
    read_capability = "synthetic-finalization-read-capability-1234"

    def provider_reply(request):
        assert request.headers["authorization"] == f"Bearer {model_key}"
        payload = json.loads(request.content)
        stage = "finalization" if "response_format" in payload else "research"
        requests.append((request.url, stage))
        body = offline_reply(request).json()
        index = len(requests)
        body.update(id=f"chatcmpl-synthetic-{index}", system_fingerprint="fp_synthetic", service_tier="default")
        body["usage"] = {
            "prompt_tokens": 30 * index, "completion_tokens": 10, "total_tokens": 30 * index + 10,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
            "completion_tokens_details": {
                "reasoning_tokens": 0, "audio_tokens": 0, "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
        }
        [choice] = body["choices"]
        choice["logprobs"] = None
        message = choice["message"]
        message.update(refusal=None, annotations=[])
        if stage == "finalization":
            assert payload["response_format"]["json_schema"]["strict"] is True
            assert "tools" not in payload
            analysis = {
                "summary": private, "relevant_components": ["repository"], "assumptions": [],
                "blocked_reason": None,
            }
            if scenario == "blocked":
                analysis["blocked_reason"] = f"synthetic blocking detail {private}"
            if scenario == "blank-summary":
                analysis["summary"] = " "
            if scenario == "invalid-fields":
                analysis["assumptions"] = private
            message["content"] = json.dumps(analysis)
            if scenario == "invalid-json":
                message["content"] = f'{{"summary":"{private}"'
            if scenario in ("parser-exception", "refusal"):
                message["content"] = None
            if scenario == "refusal":
                message["refusal"] = private
        return httpx.Response(200, json=body)

    async def inject_graph_after_finalization(graph, *args, **kwargs):
        result = await graph_invoke(graph, *args, **kwargs)
        assert "final_result" in result
        if scenario == "unexpected-runtime":
            raise RuntimeError(private)
        return {**result, "finalization_calls": 0}

    async def read_before_cleanup(factory, workspace_id, task_id, run_id):
        try:
            async with factory() as session:
                run = await session.get(smoke.AgentRunModel, run_id)
                events = (await session.scalars(select(smoke.AgentEventModel).where(
                    smoke.AgentEventModel.agent_run_id == run_id,
                    smoke.AgentEventModel.event_type.in_(("agent_execution_failed", "agent_result_recorded")),
                ))).all()
                persisted.append((run.status, [(event.event_type, event.payload) for event in events]))
        finally:
            await cleanup(factory, workspace_id, task_id, run_id)

    if scenario in ("invalid-graph-result", "unexpected-runtime"):
        monkeypatch.setattr(CompiledStateGraph, "ainvoke", inject_graph_after_finalization)
    monkeypatch.setattr(smoke, "offline_reply", provider_reply)
    monkeypatch.setattr(smoke, "cleanup", read_before_cleanup)
    monkeypatch.setattr(smoke.httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("dry-run created a live transport"))
    tokens = iter((model_key, read_capability))
    monkeypatch.setattr(smoke, "token_urlsafe", lambda _: next(tokens))
    verify = AsyncMock(side_effect=AssertionError("failed run reached verification"))
    if failure:
        monkeypatch.setattr(smoke.VerificationService, "verify", verify)

    assert smoke.main(["--dry-run"]) == int(failure is not None)
    output = capsys.readouterr()
    assert output.out, output.err
    report = json.loads(output.out)
    assert report.get("failure_code") == failure
    assert report.get("failure_phase") == phase
    assert report.get("error_type") == error_type
    assert report.get("preliminary_status") == ("blocked" if scenario == "blocked" else None)
    assert report["last_model_stage"] == "finalization"
    assert report["model_requests"] == 3 and report["tool_calls"] == 1
    assert report["tool_call_audit"] == [{"tool_name": "read_file", "status": "succeeded"}]
    assert report["fixture_cleanup"] == "completed"
    assert report["verification"] == (None if failure else "pass")
    assert report["usage"]["usage_complete"] is complete
    assert report["usage"]["known_total_tokens"] == (210 if complete else 110)
    assert len(persisted) == 1
    status, events = persisted[0]
    if scenario in ("valid", "blocked"):
        expected = "blocked" if scenario == "blocked" else "ready_for_verification"
        assert events == [("agent_result_recorded", {"schema_version": 1, "result_status": expected})]
        assert status == ("failed" if failure else "succeeded")
    else:
        assert status == "failed"
        assert events == [("agent_execution_failed", {"schema_version": 1, "code": failure})]
    assert requests == [
        (httpx.URL(f"{smoke.OFFLINE_URL}/chat/completions"), stage) for stage in ("research", "research", "finalization")
    ]
    if failure:
        verify.assert_not_called()
    logs = caplog.text + agent_log_capture.getvalue() + repr([record.__dict__ for record in http_logs])
    for marker in (private, model_key, read_capability, smoke.GOAL, smoke.CONTENT.decode().strip(), "Authorization"):
        assert marker not in output.out + output.err + logs


def test_failure_diagnostic_does_not_expose_unlisted_exception_class_or_message():
    class SyntheticPrivateError(RuntimeError):
        pass

    diagnostic = smoke.FailureDiagnostic()
    diagnostic.record(smoke.FailurePhase.UNKNOWN, SyntheticPrivateError("synthetic-private-exception"))
    assert diagnostic.error_type == "unknown"
    assert diagnostic.phase is smoke.FailurePhase.UNKNOWN


@pytest.mark.parametrize("extra_field", [False, True], ids=["unknown-code", "noncanonical-payload"])
def test_tool_finish_drops_untrusted_event_fields(extra_field):
    call = smoke.ToolCallModel(id=uuid4(), tool_name="read_file")
    status = smoke.ToolCallStatus.FAILED
    audit = smoke.ToolCallAudit(call.id, "read_file", 1, status, smoke.ToolCallErrorCode.INVALID_INPUT)
    _, payload = smoke.encode_tool_call_event(audit)
    assert smoke.decode_tool_finish(call, status, payload) == audit
    field = "unexpected" if extra_field else "error_code"
    assert smoke.decode_tool_finish(call, status, {**payload, field: "synthetic-private-value"}) is None
    assert smoke.decode_tool_finish(call, smoke.ToolCallStatus.SUCCEEDED, payload) is None


@pytest.mark.skipif(
    os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated Week 4 PostgreSQL fixture",
)
@pytest.mark.parametrize(
    ("case", "status", "error_code", "phase", "denial"),
    [
        ("invalid-input", "failed", "invalid_input", "before_handler", None),
        ("absolute-path", "denied", "permission_denied", "before_handler", PermissionReason.INVALID_PATH),
        ("protected-path", "denied", "permission_denied", "before_handler", PermissionReason.PROTECTED_PATH),
        ("out-of-scope", "denied", "permission_denied", "before_handler", PermissionReason.PATH_OUT_OF_SCOPE),
        ("handler-error", "failed", "handler_failed", "tool_execution", None),
        ("invalid-output", "failed", "invalid_output", "after_handler", None),
        ("audit-finish-failure", "running", None, "unknown", None),
        ("after-tool-success", "succeeded", None, "after_tool_success", None),
    ],
)
def test_offline_read_tool_failure_audit_before_cleanup(
    monkeypatch, capsys, caplog, agent_log_capture, http_logs, case, status, error_code, phase, denial,
):
    private = "synthetic-private-tool-argument"
    arguments = {
        "invalid-input": {"path": {"unexpected": private}},
        "absolute-path": {"path": f"/Users/{private}/sample.txt"},
        "protected-path": {"path": f".git/{private}"},
        "out-of-scope": {"path": f"{private}.txt"},
    }.get(case, {"path": smoke.FILE})
    requests = []
    reads = []
    denials = []
    cleaned = []
    happy_reply = smoke.offline_reply
    read_text = SafeRepositoryReader.read_text
    evaluate = PermissionPolicy.evaluate
    append = AgentEventRepository.append
    cleanup = smoke.cleanup

    def reply(request):
        requests.append(request.url)
        assert len(requests) == 1
        payload = happy_reply(request).json()
        message = payload["choices"][0]["message"]
        message["content"] = f"synthetic-provider-response-{private}"
        message["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=payload)

    def observe_policy(self, *args, **kwargs):
        decision = evaluate(self, *args, **kwargs)
        if decision.outcome is PermissionOutcome.DENY:
            denials.append(decision.reason)
        return decision

    def observe_read(self, path, *, spec):
        reads.append(path)
        if case == "handler-error":
            raise RepositoryError(f"/Users/{private}/repository-read")
        return read_text(self, path, spec=spec)

    async def fail_finish_event(repository, run_id, *, event_type, payload):
        if event_type == "tool_call_finished":
            raise RuntimeError(f"synthetic audit failure {private}")
        return await append(repository, run_id, event_type=event_type, payload=payload)

    async def check_cleanup(factory, workspace_id, task_id, run_id):
        call_rows = select(smoke.ToolCallModel).where(smoke.ToolCallModel.agent_run_id == run_id)
        events = select(smoke.AgentEventModel).where(smoke.AgentEventModel.agent_run_id == run_id)
        async with factory() as session:
            [call] = (await session.scalars(call_rows)).all()
            assert (call.tool_name, call.status) == ("read_file", status)
            lifecycle = (await session.scalars(events.order_by(smoke.AgentEventModel.sequence))).all()
            starts = [event for event in lifecycle if event.event_type == "tool_call_started"]
            finishes = [event for event in lifecycle if event.event_type == "tool_call_finished"]
            assert len(starts) == int(phase != "before_handler")
            assert len(finishes) == int(status != "running")
            if finishes:
                assert finishes[0].payload.get("error_code") == error_code
        await cleanup(factory, workspace_id, task_id, run_id)
        async with factory() as session:
            assert await session.scalar(call_rows) is None
            assert await session.scalar(events) is None
        cleaned.append(run_id)

    if case == "after-tool-success":
        monkeypatch.setattr(repository_graph, "MAX_TOOL_MESSAGE_BYTES", 1)
    if case == "audit-finish-failure":
        monkeypatch.setattr(AgentEventRepository, "append", fail_finish_event)
    if case == "invalid-output":
        from app.tools.repository import factory as repository_factory

        async def invalid_read_output(*args, **kwargs):
            await real_handler(*args, **kwargs)
            return {"private": private}

        real_handler = repository_factory.read_file
        monkeypatch.setattr(repository_factory, "read_file", invalid_read_output)
    monkeypatch.setattr(smoke, "offline_reply", reply)
    monkeypatch.setattr(smoke, "cleanup", check_cleanup)
    monkeypatch.setattr(smoke.httpx, "AsyncHTTPTransport", lambda **_: pytest.fail("dry-run created a live transport"))
    monkeypatch.setattr(PermissionPolicy, "evaluate", observe_policy)
    monkeypatch.setattr(SafeRepositoryReader, "read_text", observe_read)
    verify = AsyncMock(side_effect=AssertionError("failed execution reached verification"))
    monkeypatch.setattr(smoke.VerificationService, "verify", verify)
    key, capability = "synthetic-model-key", "synthetic-local-capability-redaction-marker"
    tokens = iter((key, capability))
    monkeypatch.setattr(smoke, "token_urlsafe", lambda _: next(tokens))

    assert smoke.main(["--dry-run"]) == 1
    output = capsys.readouterr()
    assert output.out, output.err
    report = json.loads(output.out)
    expected = {"tool_name": "read_file", "status": status}
    if error_code is not None:
        expected["error_code"] = error_code
    assert report["tool_call_audit"] == [expected]
    assert report["failure_code"] == "graph_execution_failed"
    assert report["failure_phase"] == phase
    assert report["outcome"] == "OFFLINE_FAILED"
    assert report["verification"] is None
    assert report["fixture_cleanup"] == "completed"
    assert report["tool_calls"] == report["model_requests"] == len(cleaned) == 1
    assert requests == [httpx.URL(f"{smoke.OFFLINE_URL}/chat/completions")]
    assert reads == ([] if phase == "before_handler" else [smoke.FILE])
    assert denials == ([] if denial is None else [denial])
    verify.assert_not_called()

    logs = caplog.text + agent_log_capture.getvalue() + repr([record.__dict__ for record in http_logs])
    for marker in (private, key, capability, smoke.GOAL, smoke.CONTENT.decode().strip(), "Authorization"):
        assert marker not in output.out + output.err + logs
