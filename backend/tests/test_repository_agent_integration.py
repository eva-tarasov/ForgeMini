"""Opt-in repository agent tests against a dedicated migrated PostgreSQL database."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from model_test_support import TRACING_OFF
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_agent_observability import agent_log_capture

from app.agent_runtime.structured_output import TaskAnalysis
from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope
from app.repositories.agent_events import AgentEventRepository
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_runner import AgentRunnerError
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError, VerificationEvidenceRequiredError
from app.services.agent_service import AgentService
from app.services.repository_agent_runner import RepositoryAgentRunner, RepositoryFixtureBinding
from app.tools.repository import factory as fixture_factory
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.filesystem import SafeRepositoryReader


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="set FORGE_REPOSITORY_AGENT_TEST_DB and POSTGRES_DB to opt in to Step 09A",
)

CONTENT = "synthetic-private-fixture-evidence Ω"
FILE = "allowed.txt"
CALL_ID = "model-repository-read-1"
API_KEY = "synthetic-repository-test-key"
GOAL = "Inspect one disposable fixture"
CRITERIA = ("Leave independent verification pending",)


@dataclass(frozen=True)
class SeededRun:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    binding: RepositoryFixtureBinding
    run_id: UUID
    task_id: UUID

    def service(self, model, *, pricing=None):
        return AgentService(
            self.factory, RepositoryAgentRunner(self.factory, model, self.binding),
            execution_timeout_seconds=10, pricing=pricing,
        )


@pytest.fixture
def database(tmp_path, monkeypatch):
    name = os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_week4_audit_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB must match FORGE_REPOSITORY_AGENT_TEST_DB and the dedicated DB guard", pytrace=False)
    settings = Settings(postgres_db=name)
    for key, value in TRACING_OFF.items():
        monkeypatch.setenv(key, value)

    for path in (FILE, "denied.txt"):
        (tmp_path / path).write_text(CONTENT, encoding="utf-8")

    async def check(scenario):
        engine = create_engine(settings)
        setup = create_session_factory(engine)
        open_sessions = set()

        class TrackedSession(AsyncSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                open_sessions.add(self)

            async def close(self):
                try:
                    await super().close()
                finally:
                    open_sessions.remove(self)

        factory = async_sessionmaker(engine, class_=TrackedSession, expire_on_commit=False)
        workspace_id, task_id, run_id = uuid4(), uuid4(), uuid4()
        binding = RepositoryFixtureBinding(
            workspace_id, tmp_path, FixtureBaseline("fixture-v1", {FILE: "previous\n"}),
            frozenset({Capability.REPOSITORY_READ}), (PathScope(FILE),),
        )
        case = SeededRun(settings, factory, binding, run_id, task_id)
        seeded = False

        def assert_no_open_session():
            assert not open_sessions and engine.pool.checkedout() == 0

        try:
            async with asyncio.timeout(25):
                async with setup.begin() as session:
                    assert await session.scalar(text("SELECT current_database()")) == name
                    assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                    assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "a7c3e91d4b20"
                    session.add(WorkspaceModel(id=workspace_id, name="synthetic repository workspace"))
                    await session.flush()
                    session.add(TaskModel(id=task_id, workspace_id=workspace_id, goal=GOAL,
                                          acceptance_criteria=list(CRITERIA), status="queued"))
                    await session.flush()
                    session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status="queued"))
                    await session.flush()
                    seeded = True
                await scenario(case, assert_no_open_session)
                assert_no_open_session()
        finally:
            try:
                if seeded:
                    async with asyncio.timeout(5), setup.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == run_id)
                        await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
                        await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == run_id))
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                        await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                        await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
            finally:
                await engine.dispose()

    def run(scenario):
        with tracing_context(enabled=False, parent=False):
            asyncio.run(check(scenario))

    return run


@asynccontextmanager
async def _offline_model(reply):
    calls = []
    base_url = "https://forge-step09a.invalid/v1"

    async def respond(request):
        assert request.method == "POST" and request.url == httpx.URL(f"{base_url}/chat/completions")
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        calls.append(json.loads(request.content))
        finish, message = await reply(len(calls) - 1, calls[-1])
        return httpx.Response(200, json={
            "id": f"chatcmpl-repository-{len(calls)}", "object": "chat.completion", "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
            "usage": {
                "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        })

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport, trust_env=False) as client:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as async_client:
            yield ChatOpenAI(
                model="gpt-4o-mini", api_key=API_KEY, base_url=base_url, max_retries=0, use_responses_api=False,
                openai_proxy="", http_client=client, http_async_client=async_client,
            ), calls


def _read_reply(path=FILE):
    return "tool_calls", {"content": None, "tool_calls": [{
        "id": CALL_ID, "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
    }]}


def _first_reply(assert_idle, path=FILE):
    async def reply(index, _payload):
        assert index == 0, "A failed or cancelled tool call must stop the graph"
        assert_idle()
        return _read_reply(path)

    return reply


async def _snapshot(factory, case):
    async with factory() as session:
        run = await session.get(AgentRunModel, case.run_id)
        task = await session.get(TaskModel, case.task_id)
        calls = (await session.scalars(
            select(ToolCallModel).where(ToolCallModel.agent_run_id == case.run_id)
        )).all()
        events = (await session.scalars(
            select(AgentEventModel).where(AgentEventModel.agent_run_id == case.run_id).order_by(AgentEventModel.sequence)
        )).all()
        return run, task, calls, events


def _assert_audit(call, events, statuses, *, error=None):
    assert call.tool_name == "read_file" and call.status == statuses[-1]
    audit = [event for event in events if event.event_type.startswith("tool_call_")]
    assert [event.payload["status"] for event in audit] == list(statuses)
    assert [event.event_type for event in audit] == [
        {"proposed": "tool_call_proposed", "running": "tool_call_started"}.get(status, "tool_call_finished")
        for status in statuses
    ]
    for event in audit:
        payload = event.payload
        expected = {
            "schema_version": 1, "tool_call_id": str(call.id), "tool_name": "read_file",
            "tool_version": 1, "status": payload["status"],
        }
        if event is audit[-1] and error is not None:
            expected["error_code"] = error
        if event is audit[-1] and "running" in statuses and payload["status"] != "running":
            assert type(payload["duration_ms"]) is int and payload["duration_ms"] >= 0
            expected["duration_ms"] = payload["duration_ms"]
        assert payload == expected


def _forbid_read(monkeypatch):
    def unexpected_read(*args, **kwargs):
        pytest.fail("The blocked request reached the fixture read handler")

    monkeypatch.setattr(SafeRepositoryReader, "read_text", unexpected_read)


def test_postgres_repository_agent_reads_fixture_and_persists_preliminary_result(database, monkeypatch, agent_log_capture):
    async def check(case, assert_idle):
        reading_engine = create_engine(case.settings)
        fresh_factory = create_session_factory(reading_engine)
        read_calls = []
        original_handler = fixture_factory.read_file
        original_read = SafeRepositoryReader.read_text
        research = "The disposable fixture supplied evidence for preliminary analysis."
        analysis = TaskAnalysis(
            summary="Fixture evidence was inspected", relevant_components=["repository"],
            assumptions=["Independent verification pending"], blocked_reason=None,
        )

        def record_read(reader, path, *, spec):
            assert_idle()
            read_calls.append((reader._root, reader._context, path))
            return original_read(reader, path, spec=spec)

        async def observe_handler(arguments, *, reader, spec):
            assert_idle()
            run, _, (call,), events = await _snapshot(fresh_factory, case)
            assert run.status == "running"
            _assert_audit(call, events, ("proposed", "running"))
            return await original_handler(arguments, reader=reader, spec=spec)

        monkeypatch.setattr(SafeRepositoryReader, "read_text", record_read)
        monkeypatch.setattr(fixture_factory, "read_file", observe_handler)

        async def reply(index, payload):
            assert_idle()
            if index == 0:
                assert payload["parallel_tool_calls"] is False
                assert [item["function"]["name"] for item in payload["tools"]] == [
                    "list_files", "read_file", "search_files", "git_diff",
                ]
                assert json.loads(payload["messages"][-1]["content"]) == {
                    "goal": GOAL, "acceptance_criteria": list(CRITERIA),
                }
                return _read_reply()
            if index == 1:
                message = payload["messages"][-1]
                assert message["role"] == "tool" and message["tool_call_id"] == CALL_ID
                assert json.loads(message["content"]) == {
                    "path": FILE, "content": CONTENT, "size_bytes": len(CONTENT.encode()),
                }
                return "stop", {"content": research}
            assert index == 2 and payload["response_format"]["type"] == "json_schema"
            assert not payload.get("tools")
            assert json.loads(payload["messages"][-1]["content"]) == {
                "goal": GOAL, "acceptance_criteria": list(CRITERIA), "analysis_notes": [research], "observations": [],
            }
            return "stop", {"content": analysis.model_dump_json()}

        tariff = ModelTariff("openai", "gpt-4o-mini", "fixture-v1", Decimal("1"), Decimal("2"))
        try:
            async with _offline_model(reply) as (model, calls):
                service = case.service(model, pricing=AgentPricing(tariff, tariff))
                result = await service.execute_run(case.run_id)
                assert len(calls) == 3
                reader = AgentService(fresh_factory, RepositoryAgentRunner(fresh_factory, model, case.binding))
                assert await reader.get_run_result(case.run_id) == result
                assert await reader.get_run_failure(case.run_id) is None
                usage = await reader.get_run_usage(case.run_id)

            assert result.status == "ready_for_verification" and result.observations == ()
            assert result.summary == analysis.summary
            assert usage.snapshot.reported_calls == 3 and usage.snapshot.known_total_tokens == 90
            assert usage.cost_estimate.cost_complete and usage.cost_estimate.known_cost_usd == Decimal("0.00012")

            (root, context, path), = read_calls
            assert (root, path) == (case.binding.root.resolve(), FILE)
            assert (context.run_id, context.workspace_id) == (case.run_id, case.binding.workspace_id)
            assert context.read_scopes == (PathScope(FILE),) and context.write_scopes == ()

            run, task, (call,), events = await _snapshot(fresh_factory, case)
            assert (run.status, task.status) == ("verifying", "queued")
            assert run.final_result["schema_version"] == 1 and run.final_result["result"]["observations"] == []
            assert isinstance(call.id, UUID) and str(call.id) != CALL_ID
            _assert_audit(call, events, ("proposed", "running", "succeeded"))
            assert [(event.sequence, event.event_type) for event in events] == list(enumerate((
                "agent_run_status_changed", "tool_call_proposed", "tool_call_started", "tool_call_finished",
                "agent_result_recorded", "agent_run_status_changed", "agent_usage_recorded",
            ), 1))
            assert events[0].payload == {"from": "queued", "to": "running"}
            assert events[5].payload == {"from": "running", "to": "verifying"}
            async with fresh_factory() as session:
                with pytest.raises(VerificationEvidenceRequiredError):
                    await AgentRunService(session).transition_status(
                        case.run_id, AgentRunStatus.VERIFYING, AgentRunStatus.SUCCEEDED,
                    )

            logs = agent_log_capture.getvalue()
            assert "agent_usage_observed" in logs
            for secret in (CONTENT, FILE, CALL_ID, str(case.binding.root), API_KEY):
                assert secret not in logs
                assert secret not in json.dumps([event.payload for event in events], ensure_ascii=False)
                assert secret not in json.dumps(run.final_result, ensure_ascii=False)
        finally:
            await reading_engine.dispose()

    database(check)


def test_postgres_repository_agent_denies_read_before_handler(database, monkeypatch, agent_log_capture):
    _forbid_read(monkeypatch)

    async def check(case, assert_idle):
        async with _offline_model(_first_reply(assert_idle, "denied.txt")) as (model, calls):
            with pytest.raises(AgentRunnerError, match="^graph_execution_failed$") as error:
                await case.service(model).execute_run(case.run_id)
        assert error.value.__cause__.args == ("permission_denied",)
        assert len(calls) == 1
        run, task, (call,), events = await _snapshot(case.factory, case)
        assert (run.status, run.final_result, task.status) == ("failed", None, "queued")
        _assert_audit(call, events, ("proposed", "denied"), error="permission_denied")
        for value in ("denied.txt", CALL_ID, CONTENT):
            assert value not in agent_log_capture.getvalue()
            assert value not in json.dumps([event.payload for event in events])

    database(check)


def test_postgres_repository_agent_cancellation_stops_audited_handler(database, monkeypatch):
    async def check(case, assert_idle):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def wait_in_handler(*args, **kwargs):
            assert_idle()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(fixture_factory, "read_file", wait_in_handler)
        async with _offline_model(_first_reply(assert_idle)) as (model, calls):
            service = case.service(model)
            execution = asyncio.create_task(service.execute_run(case.run_id))
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                await service.cancel_run(case.run_id)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(execution, timeout=3)
            finally:
                if not execution.done():
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
        assert cleaned.is_set() and len(calls) == 1
        run, task, (call,), events = await _snapshot(case.factory, case)
        assert (run.status, run.final_result, task.status) == ("cancelled", None, "queued")
        _assert_audit(call, events, ("proposed", "running", "failed"), error="cancelled")

    database(check)


def test_postgres_repository_agent_stale_parent_blocks_admission(database, monkeypatch):
    _forbid_read(monkeypatch)

    async def check(case, assert_idle):
        async def cancel_then_request(index, _payload):
            assert index == 0
            assert_idle()
            async with case.factory() as session:
                await AgentRunService(session).cancel_execution(case.run_id)
            return _read_reply()

        async with _offline_model(cancel_then_request) as (model, calls):
            with pytest.raises(StaleAgentRunStatusError):
                await case.service(model).execute_run(case.run_id)
        assert len(calls) == 1
        run, task, tool_calls, events = await _snapshot(case.factory, case)
        assert (run.status, run.final_result, task.status) == ("cancelled", None, "queued")
        assert not tool_calls
        assert not any(event.event_type.startswith("tool_call_") for event in events)

    database(check)


def test_postgres_repository_agent_audit_failure_prevents_handler(database, monkeypatch):
    _forbid_read(monkeypatch)
    original = AgentEventRepository.append
    attempted = []

    async def invalid_start(repo, run_id, *, event_type, payload):
        event = await original(repo, run_id, event_type=event_type, payload=payload)
        if event_type == "tool_call_started":
            attempted.append(event_type)
            event.sequence = 0
            await repo.session.flush()
        return event

    monkeypatch.setattr(AgentEventRepository, "append", invalid_start)

    async def check(case, assert_idle):
        async with _offline_model(_first_reply(assert_idle)) as (model, calls):
            with pytest.raises(AgentRunnerError, match="^graph_execution_failed$") as error:
                await case.service(model).execute_run(case.run_id)
        assert error.value.__cause__.args == ("audit_persistence_failed",)
        assert attempted == ["tool_call_started"] and len(calls) == 1
        run, task, (call,), events = await _snapshot(case.factory, case)
        assert (run.status, run.final_result, task.status) == ("failed", None, "queued")
        _assert_audit(call, events, ("proposed", "failed"), error="audit_persistence_failed")

    database(check)
