"""One read-only Forge smoke against the isolated Week 4 PostgreSQL fixture."""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_core.exceptions import ModelRefusalError, OutputParserException
from langchain_core.messages import AIMessage, BaseMessage, InvalidToolCall
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from langgraph.errors import InvalidUpdateError
from langsmith import tracing_context
from openai import (
    APIConnectionError, APIStatusError, APITimeoutError, ContentFilterFinishReasonError, LengthFinishReasonError,
)
from pydantic import PrivateAttr, SecretStr, ValidationError
from sqlalchemy import delete, select, text

from app.agent_runtime import model as model_factory
from app.agent_runtime.agent_graph import MAX_FINALIZATION_CALLS, MAX_MODEL_CALLS, MAX_TOOL_CALLS, AgentGraphError
from app.agent_runtime.finalization import FinalizationError
from app.agent_runtime.repository_tool_schemas import READ_TOOL_NAMES
from app.agent_runtime.settings import ModelSettings
from app.agent_runtime.structured_output import StructuredOutputError
from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import ToolCallStatus
from app.main import create_app
from app.permissions.context import PathScope
from app.services.agent_failure import AgentFailureCode
from app.services.agent_runner import AgentRunnerError
from app.services.agent_usage import ModelCallStage
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.repository_agent_runner import RepositoryAgentRunner, RepositoryFixtureBinding
from app.services.tool_call_audit import ToolCallAudit, ToolCallErrorCode, encode_tool_call_event
from app.services.verification_contract import (
    FileDigestCheck, VerificationBinding, VerificationPlan, VerificationVerdict, fingerprint_criteria,
)
from app.services.verification_observer import TrustedFixtureAccess
from app.services.verification_service import VerificationService
from app.tools.repository.baseline import FixtureBaseline


TEST_DB = "forge_task06_week4_audit_4d6f653603"
MODEL = "gpt-6-luna"
OFFLINE_URL = "https://forge-smoke.invalid/v1"
OPENAI_URL = "https://api.openai.com/v1"
MAX_REQUESTS = MAX_MODEL_CALLS + MAX_FINALIZATION_CALLS
MAX_OUTPUT_TOKENS = 512
MAX_REQUEST_BYTES = 16 * 1024
REQUEST_TIMEOUT = 10.0
SCENARIO_TIMEOUT = 65.0
DB_TIMEOUT = 5.0
FAILURE_USAGE_WAIT = 1.0
SMOKE_TOOL_NAME = "read_file"
FILE = "sample.txt"
CONTENT = b"synthetic-week4-fixture: sample=17\n"
GOAL = f"Use {SMOKE_TOOL_NAME} on {FILE} and provide a preliminary observation for independent verification."
CRITERIA = ("The designated synthetic fixture has the bytes specified by the trusted plan.",)
READ_ONLY = frozenset({Capability.REPOSITORY_READ})


class SmokeError(Exception):
    """A fixed diagnostic code safe to show on the command line."""


class FailurePhase(StrEnum):
    BEFORE_HANDLER = "before_handler"
    TOOL_EXECUTION = "tool_execution"
    AFTER_HANDLER = "after_handler"
    AFTER_TOOL_SUCCESS = "after_tool_success"
    FINALIZATION_MODEL = "finalization_model"
    FINALIZATION_PARSER = "finalization_parser"
    FINALIZATION_VALIDATION = "finalization_validation"
    FINALIZATION_BLOCKED = "finalization_blocked"
    GRAPH_RESULT = "graph_result"
    UNKNOWN = "unknown"


_KNOWN_ERROR_BASES = (
    (APITimeoutError, "api_timeout"),
    (APIConnectionError, "api_connection_error"),
    (APIStatusError, "api_status_error"),
    (ValidationError, "validation_error"),
    (ModelRefusalError, "model_refusal"),
    (OutputParserException, "output_parser_error"),
)
_KNOWN_ERROR_TYPES = {
    ContentFilterFinishReasonError: "content_filter_finish_reason",
    LengthFinishReasonError: "length_finish_reason",
    AgentGraphError: "graph_error",
    InvalidUpdateError: "graph_invalid_update",
    StructuredOutputError: "structured_output_error",
    FinalizationError: "finalization_error",
    KeyError: "key_error",
    TypeError: "type_error",
    ValueError: "value_error",
    RuntimeError: "runtime_error",
}


@dataclass(slots=True)
class FailureDiagnostic:
    phase: FailurePhase = FailurePhase.UNKNOWN
    error_type: str | None = None

    def record(self, phase: FailurePhase, error: Exception) -> None:
        self.phase = phase
        self.error_type = next(
            (label for kind, label in _KNOWN_ERROR_BASES if isinstance(error, kind)),
            _KNOWN_ERROR_TYPES.get(type(error), "unknown"),
        )


class SmokeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "smoke_refused_or_failed: invalid_cli_arguments\n")


def require(condition, code):
    if not condition:
        raise SmokeError(code)


def load_model_settings(live):
    if live:
        require(os.environ.get("FORGE_WEEK4_LIVE_SMOKE") == "1", "live_opt_in_required")
        require(os.environ.get("FORGE_MODEL_NAME") == MODEL, "unsupported_model")
        key = os.environ.get("OPENAI_API_KEY", "")
        require(
            key and key.isascii() and key.isprintable() and not any(c.isspace() for c in key),
            "api_key_invalid",
        )
    else:
        key = token_urlsafe(32)
    return ModelSettings(
        _env_file=None, openai_api_key=key, forge_model_name=MODEL,
        forge_model_timeout_seconds=REQUEST_TIMEOUT,
    )


def load_db_settings():
    require(
        os.environ.get("POSTGRES_HOST") == "127.0.0.1"
        and os.environ.get("POSTGRES_PORT") == "50088"
        and os.environ.get("POSTGRES_DB") == os.environ.get("FORGE_REPOSITORY_AGENT_TEST_DB") == TEST_DB,
        "dedicated_db_required",
    )
    return Settings(
        _env_file=None, postgres_host="127.0.0.1", postgres_port=50088, postgres_db=TEST_DB,
    )


async def verify_db(factory):
    async with asyncio.timeout(DB_TIMEOUT), factory.begin() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        identity = (await session.execute(text(
            "SELECT current_database(), current_setting('server_version_num'), "
            "current_setting('transaction_isolation')"
        ))).one()
        revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        require(
            identity == (TEST_DB, "170011", "read committed") and revision == "1d972cc34fbe",
            "db_preflight_mismatch",
        )


class BoundedTransport(httpx.AsyncBaseTransport):
    """Check the serialized Chat Completions request before handing it to the transport."""

    def __init__(self, base_url, transport):
        self.endpoint = httpx.URL(f"{base_url}/chat/completions")
        self.transport = transport
        self.calls = 0
        self.last_model_stage = None
        self.refusal_code = None

    async def handle_async_request(self, request):
        try:
            stage = self.check_request(request, await request.aread())
        except SmokeError as error:
            self.refusal_code = str(error)
            raise
        self.calls += 1
        self.last_model_stage = stage
        return await self.transport.handle_async_request(request)

    def check_request(self, request, content) -> ModelCallStage:
        require(request.method == "POST" and request.url == self.endpoint, "unexpected_model_endpoint")
        require(len(content) <= MAX_REQUEST_BYTES, "model_request_too_large")
        payload = json.loads(content)
        require(
            payload.get("model") == MODEL
            and payload.get("reasoning_effort") == "none"
            and type(payload.get("max_completion_tokens")) is int
            and payload.get("max_completion_tokens") == MAX_OUTPUT_TOKENS
            and "max_tokens" not in payload
            and "n" not in payload
            and not payload.get("stream"),
            "invalid_model_request_budget",
        )
        tools = payload.get("tools", [])
        if "response_format" in payload:
            response_format = payload["response_format"]
            require(
                not tools and response_format.get("type") == "json_schema"
                and response_format.get("json_schema", {}).get("strict") is True,
                "invalid_structured_output_request",
            )
        else:
            require(
                payload.get("parallel_tool_calls") is False
                and tuple(tool["function"]["name"] for tool in tools) == (SMOKE_TOOL_NAME,)
                and all(tool["type"] == "function" for tool in tools),
                "read_only_tools_required",
            )
        require(self.calls < MAX_REQUESTS, "model_call_budget_exceeded")
        return ModelCallStage.FINALIZATION if "response_format" in payload else ModelCallStage.RESEARCH

    async def aclose(self):
        await self.transport.aclose()


class ClosedSyncTransport(httpx.BaseTransport):
    def handle_request(self, request):
        raise SmokeError("synchronous_model_request_denied")


def reject_unadvertised_tool_calls(message: BaseMessage) -> BaseMessage:
    if isinstance(message, AIMessage) and any(call["name"] != SMOKE_TOOL_NAME for call in message.tool_calls):
        # Preserve usage while taking the graph's existing invalid-tool-call path.
        return AIMessage(
            content="",
            invalid_tool_calls=[InvalidToolCall(error="smoke_tool_not_advertised")],
            usage_metadata=message.usage_metadata,
        )
    return message


class SmokeChatOpenAI(ChatOpenAI):
    _failure_diagnostic: FailureDiagnostic = PrivateAttr(default_factory=FailureDiagnostic)

    def bind_tools(self, tools, **kwargs):
        # The runner already supplied schemas derived from the registered ToolSpecs.
        selected = [
            tool for tool in tools
            if isinstance(tool, dict) and tool.get("type") == "function"
            and isinstance(tool.get("function"), dict) and tool["function"].get("name") == SMOKE_TOOL_NAME
        ]
        require(len(selected) == 1, "smoke_read_file_schema_required")
        return super().bind_tools(selected, **kwargs) | RunnableLambda(reject_unadvertised_tool_calls)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as error:
            if "response_format" in kwargs:
                self._failure_diagnostic.record(FailurePhase.FINALIZATION_MODEL, error)
                # The SDK can reject a structured completion before Forge gets a parsed envelope.
                if isinstance(error, (ContentFilterFinishReasonError, LengthFinishReasonError, ValidationError)):
                    raise StructuredOutputError("sdk_structured_parse_failed") from None
            raise

    def with_structured_output(self, schema=None, **kwargs):
        runnable = super().with_structured_output(schema, **kwargs)
        if kwargs.get("method") != "json_schema" or kwargs.get("include_raw") is not True:
            return runnable

        def observe_parser(result):
            if isinstance(result, Mapping) and isinstance(error := result.get("parsing_error"), Exception):
                self._failure_diagnostic.record(FailurePhase.FINALIZATION_PARSER, error)
            return result

        return runnable | RunnableLambda(observe_parser)


class SmokeRepositoryAgentRunner(RepositoryAgentRunner):
    def __init__(self, factory, model: SmokeChatOpenAI, binding):
        super().__init__(factory, model, binding)
        self._failure_diagnostic = model._failure_diagnostic

    async def run(self, request):
        try:
            return await super().run(request)
        except Exception as error:
            cause = error.__cause__ if isinstance(error, AgentRunnerError) else error
            if self._failure_diagnostic.error_type is None and isinstance(cause, Exception):
                phase = FailurePhase.UNKNOWN
                if isinstance(cause, FinalizationError):
                    phase = FailurePhase.FINALIZATION_VALIDATION
                elif isinstance(cause, StructuredOutputError):
                    phase = FailurePhase.FINALIZATION_PARSER
                elif type(cause) is AgentGraphError and cause.args == ("invalid_finalization_result",):
                    phase = FailurePhase.GRAPH_RESULT
                self._failure_diagnostic.record(phase, cause)
            raise


def create_bounded_model(settings, base_url, sync_client, async_client):
    def constructor(**kwargs):
        return SmokeChatOpenAI(
            **kwargs, base_url=base_url, http_client=sync_client, http_async_client=async_client,
            openai_proxy="", use_responses_api=False, reasoning_effort="none",
            max_completion_tokens=MAX_OUTPUT_TOKENS,
        )

    # Scope injection to the factory's synchronous constructor call. Requests still use the real SDK.
    with patch.object(model_factory, "ChatOpenAI", constructor):
        model = model_factory.create_openai_model(settings)
    require(model.max_retries == 0, "model_retries_enabled")
    return model


def offline_reply(request):
    payload = json.loads(request.content)
    history = payload["messages"]
    finish = "stop"
    message = {"role": "assistant"}
    if "response_format" in payload:
        message["content"] = json.dumps({
            "summary": "Synthetic sample observed",
            "relevant_components": ["repository"],
            "assumptions": ["Independent verification is pending"],
            "blocked_reason": None,
        })
    elif history[-1]["role"] == "tool":
        require("sample=17" in history[-1]["content"], "expected_fixture_observation_missing")
        message["content"] = "Synthetic tool observation received."
    else:
        finish = "tool_calls"
        message["tool_calls"] = [{
            "id": "synthetic-read-1", "type": "function",
            "function": {"name": SMOKE_TOOL_NAME, "arguments": json.dumps({"path": FILE})},
        }]
    return httpx.Response(200, json={
        "id": "chatcmpl-forge-smoke", "object": "chat.completion", "created": 1, "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    })


def build_runner(factory, model, binding):
    require(
        binding.capabilities == READ_ONLY and binding.read_scopes == (PathScope(FILE),),
        "read_only_fixture_required",
    )
    return SmokeRepositoryAgentRunner(factory, model, binding)


async def cleanup(factory, workspace_id, task_id, run_id):
    async with asyncio.timeout(DB_TIMEOUT), factory.begin() as session:
        call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == run_id)
        await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
        for model in (ToolCallModel, AgentEventModel):
            await session.execute(delete(model).where(model.agent_run_id == run_id))
        for model, row_id in ((AgentRunModel, run_id), (TaskModel, task_id), (WorkspaceModel, workspace_id)):
            await session.execute(delete(model).where(model.id == row_id))


def response_body(response, status=200):
    require(response.status_code == status, "unexpected_forge_http_status")
    return response.json()


@dataclass(frozen=True, slots=True)
class ObservedToolCall:
    tool_name: str
    status: ToolCallStatus
    finished: ToolCallAudit | None

    def safe_details(self) -> dict[str, str]:
        details = {"tool_name": self.tool_name, "status": self.status.value}
        if self.finished is not None and self.finished.error_code is not None:
            details["error_code"] = self.finished.error_code.value
        return details


def decode_tool_finish(call: ToolCallModel, status: ToolCallStatus, payload: object) -> ToolCallAudit | None:
    if type(payload) is not dict or type(payload.get("schema_version")) is not int:
        return None
    try:
        audit = ToolCallAudit(
            call.id, call.tool_name, payload["tool_version"], status,
            ToolCallErrorCode(payload["error_code"]) if "error_code" in payload else None,
            payload.get("duration_ms"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return audit if encode_tool_call_event(audit) == ("tool_call_finished", payload) else None


async def read_tool_audit(factory, run_id) -> list[ObservedToolCall]:
    async with factory() as session:
        tool_calls = select(ToolCallModel).where(ToolCallModel.agent_run_id == run_id)
        calls = (await session.scalars(tool_calls.limit(MAX_TOOL_CALLS + 1))).all()
        approval = await session.scalar(select(ApprovalModel.id).where(
            ApprovalModel.tool_call_id.in_(tool_calls.with_only_columns(ToolCallModel.id))
        ).limit(1))
        require(
            approval is None and len(calls) <= MAX_TOOL_CALLS
            and all(call.tool_name in READ_TOOL_NAMES for call in calls),
            "unexpected_tool_audit",
        )
        observed = []
        for call in calls:
            status = ToolCallStatus(call.status)
            finished_events = (await session.scalars(select(AgentEventModel.payload).where(
                AgentEventModel.agent_run_id == run_id,
                AgentEventModel.event_type == "tool_call_finished",
                AgentEventModel.payload["tool_call_id"].astext == str(call.id),
            ).limit(2))).all()
            finished = decode_tool_finish(call, status, finished_events[0]) if len(finished_events) == 1 else None
            observed.append(ObservedToolCall(call.tool_name, status, finished))
        return observed


def graph_failure_phase(code: AgentFailureCode | None, calls: list[ObservedToolCall]) -> FailurePhase:
    if code is not AgentFailureCode.GRAPH_EXECUTION_FAILED or len(calls) != 1 or calls[0].finished is None:
        return FailurePhase.UNKNOWN
    audit = calls[0].finished
    if audit.duration_ms is None:
        if audit.error_code in (
            ToolCallErrorCode.INVALID_INPUT, ToolCallErrorCode.INVALID_HANDLER,
            ToolCallErrorCode.PERMISSION_DENIED, ToolCallErrorCode.EXECUTION_BLOCKED,
        ):
            return FailurePhase.BEFORE_HANDLER
        return FailurePhase.UNKNOWN
    if audit.status is ToolCallStatus.SUCCEEDED:
        return FailurePhase.AFTER_TOOL_SUCCESS
    if audit.error_code is ToolCallErrorCode.INVALID_OUTPUT:
        return FailurePhase.AFTER_HANDLER
    if audit.error_code in (ToolCallErrorCode.HANDLER_FAILED, ToolCallErrorCode.TOOL_TIMEOUT):
        return FailurePhase.TOOL_EXECUTION
    return FailurePhase.UNKNOWN


async def observe_run(client, factory, binding, task_id, run_id, token):
    path = f"/agent-runs/{run_id}"
    start = response_body(await client.post(f"{path}/start"), 202)
    require(start == {"run_id": str(run_id), "accepted": True}, "start_not_accepted")

    while True:
        preliminary = response_body(await client.get(f"{path}/result"))
        status = preliminary["run_status"]
        if status == "failed":
            code = AgentFailureCode(raw_code) if (raw_code := preliminary["execution_failure_code"]) else None
            # Failure and usage are committed separately. Missing usage must not hide the failure code.
            if preliminary["usage"] is None:
                try:
                    async with asyncio.timeout(FAILURE_USAGE_WAIT):
                        while preliminary["usage"] is None:
                            await asyncio.sleep(0.05)
                            preliminary = response_body(await client.get(f"{path}/result"))
                except TimeoutError:
                    pass
            calls = await read_tool_audit(factory, run_id)
            report = {
                "failure_code": code.value if code is not None else "unknown", "tool_calls": len(calls),
                "failure_phase": graph_failure_phase(code, calls).value,
                "tool_call_audit": [call.safe_details() for call in calls],
                "verification": None, "usage": preliminary["usage"],
            }
            if code is None and preliminary["preliminary_status"] == "blocked":
                report.update(
                    failure_code="preliminary_blocked",
                    failure_phase=FailurePhase.FINALIZATION_BLOCKED.value,
                    preliminary_status="blocked",
                )
            return report
        require(status in {"queued", "running", "verifying"}, "unexpected_execution_status")
        require(preliminary["verification_verdict"] is None, "premature_verdict")
        if status == "verifying" and preliminary["usage"] is not None:
            break
        await asyncio.sleep(0.05)
    require(preliminary["preliminary_status"] == "ready_for_verification", "preliminary_result_missing")
    calls = await read_tool_audit(factory, run_id)
    require(
        all(
            call.tool_name == SMOKE_TOOL_NAME and call.status is ToolCallStatus.SUCCEEDED and call.finished is not None
            for call in calls
        ),
        "unexpected_tool_audit",
    )
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
    evidence_path = f"{path}/evidence"
    require(not response_body(await client.get(evidence_path, headers=headers))["items"], "premature_evidence")

    identity = VerificationBinding(run_id, task_id, binding.workspace_id, binding.baseline.revision)
    plan = VerificationPlan(
        uuid4(), identity, fingerprint_criteria(CRITERIA),
        (FileDigestCheck(uuid4(), 0, FILE, sha256(CONTENT).hexdigest()),),
    )
    access = TrustedFixtureAccess(
        plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=identity, root=binding.root,
        read_scopes=binding.read_scopes, acceptance_criteria=CRITERIA,
    )
    evidence = await VerificationService(factory).verify(plan, access, attempt_id=uuid4(), isolated_fixture=True)
    require(evidence.verdict is VerificationVerdict.PASS, "independent_verification_did_not_pass")

    final = response_body(await client.get(f"{path}/result"))
    require(
        final["run_status"] == "succeeded" and final["verification_verdict"] == "pass"
        and final["usage"] == preliminary["usage"],
        "verification_result_mismatch",
    )
    page = response_body(await client.get(f"{path}/events", params={"limit": 100}))
    events = page["items"]
    require(not page["has_more"] and len(events) >= 2, "incomplete_event_history")
    verification, terminal = events[-2:]
    require(
        verification["kind"] == "verification_recorded"
        and terminal["kind"] == "agent_run_status_changed"
        and terminal["details"] == {"from": "verifying", "to": "succeeded"}
        and sum(event["kind"] == "agent_usage_recorded" for event in events) == 1,
        "verification_event_order_mismatch",
    )
    attempts = response_body(await client.get(evidence_path, headers=headers))["items"]
    detail = response_body(await client.get(f"{evidence_path}/{evidence.attempt_id}", headers=headers))
    require(
        attempts == [{key: value for key, value in detail.items() if key != "checks"}]
        and detail["verdict"] == "pass" and detail["sequence"] == verification["sequence"]
        and detail["attempt_id"] == str(evidence.attempt_id)
        and [(check["path"], check["outcome"]) for check in detail["checks"]] == [(FILE, "match")],
        "verification_evidence_mismatch",
    )

    require(
        sum(event["kind"] == "tool_call_finished" for event in events) == len(calls)
        and not any(event["kind"].startswith("approval_") for event in events),
        "unexpected_tool_audit",
    )
    return {
        "tool_calls": len(calls), "tool_call_audit": [call.safe_details() for call in calls],
        "verification": "pass", "usage": final["usage"],
    }


async def run_scenario(factory, model, root):
    workspace_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    (root / FILE).write_bytes(CONTENT)
    binding = RepositoryFixtureBinding(
        workspace_id, root, FixtureBaseline("smoke-v1", {FILE: CONTENT.decode()}), READ_ONLY, (PathScope(FILE),),
    )
    token = SecretStr(token_urlsafe(32))
    app = create_app(
        session_factory=factory,
        execution_binding=RunExecutionBinding(workspace_id, lambda factory, _: build_runner(factory, model, binding)),
        approval_access_binding=LocalApprovalAccessBinding(workspace_id, token),
    )
    seeded = False
    try:
        async with asyncio.timeout(DB_TIMEOUT), factory.begin() as session:
            session.add(WorkspaceModel(id=workspace_id, name="Synthetic Week 4 smoke"))
            await session.flush()
            session.add(TaskModel(id=task_id, workspace_id=workspace_id, goal=GOAL, acceptance_criteria=list(CRITERIA),
                                  status="queued"))
            await session.flush()
            session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status="queued"))
            await session.flush()
            seeded = True
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(SCENARIO_TIMEOUT), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://forge-smoke.local", trust_env=False,
            ) as client:
                return await observe_run(client, factory, binding, task_id, run_id, token)
    finally:
        if seeded:
            try:
                await cleanup(factory, workspace_id, task_id, run_id)
            except Exception:
                raise SmokeError("fixture_cleanup_failed") from None


async def run_smoke(live):
    model_settings = load_model_settings(live)
    engine = create_engine(load_db_settings())
    try:
        factory = create_session_factory(engine)
        await verify_db(factory)
        base_url = OPENAI_URL if live else OFFLINE_URL
        inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False) if live else httpx.MockTransport(offline_reply)
        transport = BoundedTransport(base_url, inner)
        with TemporaryDirectory(prefix="forge-week4-smoke-") as root, tracing_context(enabled=False, parent=False):
            with httpx.Client(transport=ClosedSyncTransport(), trust_env=False) as sync_client:
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False,
                ) as async_client:
                    model = create_bounded_model(model_settings, base_url, sync_client, async_client)
                    report = await run_scenario(factory, model, Path(root))
        if transport.refusal_code is not None:
            report["failure_code"] = transport.refusal_code
            report["failure_phase"] = FailurePhase.UNKNOWN.value
        if "failure_code" in report:
            diagnostic = model._failure_diagnostic
            if transport.refusal_code is not None:
                report["error_type"] = "smoke_guard"
            elif report.get("preliminary_status") == "blocked":
                report["error_type"] = "none"
            else:
                report["error_type"] = diagnostic.error_type or "unknown"
                if diagnostic.phase is not FailurePhase.UNKNOWN:
                    report["failure_phase"] = diagnostic.phase.value
            outcome = "_FAILED"
        else:
            require(
                transport.calls == MAX_FINALIZATION_CALLS + 1 + report["tool_calls"],
                "unexpected_model_request_count",
            )
            outcome = "_PASS" if report["tool_calls"] else "_PARTIAL_NO_TOOL_CALL"
        return {
            "outcome": ("LIVE" if live else "OFFLINE") + outcome,
            "model_requests": transport.calls,
            "last_model_stage": transport.last_model_stage.value if transport.last_model_stage is not None else "unknown",
            "fixture_cleanup": "completed",
            **report,
        }
    finally:
        async with asyncio.timeout(DB_TIMEOUT):
            await engine.dispose()


def main(argv=None):
    parser = SmokeArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    for name in ("openai", "httpx", "httpcore", "langsmith"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        report = asyncio.run(run_smoke(args.run))
    except SmokeError as error:
        print(f"smoke_refused_or_failed: {error}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("smoke_refused_or_failed: local_deadline_exceeded", file=sys.stderr)
        return 1
    except Exception:
        print("smoke_refused_or_failed: configuration_or_runtime_error", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    if "failure_code" in report:
        return 1
    return 0 if report["tool_calls"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
