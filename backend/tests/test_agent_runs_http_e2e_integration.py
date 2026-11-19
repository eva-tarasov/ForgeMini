"""Exercise the public AgentRun lifecycle with trusted, offline fixture execution."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from langsmith import tracing_context
from sqlalchemy import delete, select, text
from test_agent_runs_start_cancel_integration import FINAL, _client, _command, _execution, _wait_for_cleanup
from test_approved_repository_integration import (
    ORIGINAL, READ_ID, REPLACEMENT, WRITE_ID, _fixture_patch, _runner, _tool_reply, _wait_for_preview, _write_events,
)
from test_repository_agent_integration import (
    API_KEY, CALL_ID, CONTENT, FILE, SeededRun, _assert_audit, _offline_model, _read_reply,
)
from test_task_idempotency_integration import database
from test_verification_e2e_integration import (
    ANALYSIS, APPROVED_BYTES, CRITERIA, _assert_finalized, _fresh_state, _issued_plan, _record_replacements,
)

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, ToolCallModel
from app.domain.capabilities import Capability
from app.main import create_app
from app.permissions.context import PathScope
from app.services.agent_result_codec import decode_agent_result
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError
from app.services.approval_contracts import ApprovalDecision
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.pending_approvals import PendingApprovals
from app.services.repository_agent_runner import RepositoryAgentRunner, RepositoryFixtureBinding
from app.services.verification_contract import VerificationVerdict
from app.services.verification_service import VerificationService
from app.tools.repository.baseline import FixtureBaseline


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to the dedicated fixture DB",
)

TRANSITION = "agent_run_status_changed"
READ_EVENTS = (TRANSITION, "tool_call_proposed", "tool_call_started", "tool_call_finished")
APPROVAL_EVENTS = (*READ_EVENTS, "tool_call_proposed", "approval_requested", TRANSITION)
PRELIMINARY_EVENTS = ("agent_result_recorded", TRANSITION, "agent_usage_recorded")


@pytest.fixture
def http_run(database, tmp_path, caplog):
    @asynccontextmanager
    async def open_run(build_runner):
        assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
        name = os.environ["FORGE_TASK06_TEST_DB"]
        assert name.startswith("forge_task06_week4_audit_")
        caplog.set_level(logging.INFO)
        with tracing_context(enabled=False, parent=False):
            async with database() as (factory, (workspace_id, _), task_ids):
                async with factory() as session:
                    assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                    assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "1d972cc34fbe"
                binding = RepositoryFixtureBinding(
                    workspace_id, tmp_path, FixtureBaseline("fixture-v1", {FILE: "previous\n"}),
                    frozenset({Capability.REPOSITORY_READ}), (PathScope(FILE),),
                )
                settings = Settings(postgres_db=name)
                recorded, runners = [], []
                case = None

                def compose(session_factory, run_id):
                    assert case is not None and run_id == case.run_id and session_factory is factory
                    runner = build_runner(case)
                    runners.append(runner)
                    return runner

                # Use the same app, binding and lifespan from Task creation through the last poll.
                app = create_app(
                    session_factory=factory, execution_binding=RunExecutionBinding(workspace_id, compose),
                )

                def body(response, status=200):
                    assert response.status_code == status, response.text
                    recorded.append(response.text)
                    return response.json()

                try:
                    async with _client(app) as client:
                        created = body(await client.post("/tasks", json={
                            "workspace_id": str(workspace_id), "goal": "Inspect one disposable fixture",
                            "acceptance_criteria": list(CRITERIA),
                        }), 201)
                        task_id = UUID(created["id"])
                        task_ids.add(task_id)
                        assert (created["workspace_id"], created["acceptance_criteria"]) == (
                            str(workspace_id), list(CRITERIA),
                        )
                        create_url = f"/tasks/{task_id}/runs"
                        metadata = body(await client.post(create_url), 201)
                        case = SeededRun(settings, factory, binding, UUID(metadata["id"]), task_id)
                        assert body(await client.post(create_url, json={})) == metadata
                        assert await _metadata(client, body, case, "queued") == metadata
                        assert await _events(client, body, case) == []
                        await _result(client, body, case, "queued")
                        assert not runners
                        yield app, client, case, body, runners, recorded
                        assert app.state.run_coordinator._execution is None
                        assert body(await client.get(f"/tasks/{task_id}")) == created
                        async with factory() as session:
                            assert (await session.scalars(select(AgentRunModel.id).where(
                                AgentRunModel.task_id == task_id,
                            ))).all() == [case.run_id]
                finally:
                    async with asyncio.timeout(5), factory.begin() as session:
                        run_ids = select(AgentRunModel.id).where(AgentRunModel.task_id.in_(task_ids))
                        call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id.in_(run_ids))
                        await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
                        await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id.in_(run_ids)))
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id.in_(run_ids)))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.task_id.in_(task_ids)))

    return open_run


def _url(case, suffix=""):
    return f"/agent-runs/{case.run_id}{suffix}"


async def _metadata(client, body, case, status):
    metadata = body(await client.get(_url(case)))
    assert set(metadata) == {"id", "task_id", "attempt_number", "status", "created_at"}
    assert (metadata["id"], metadata["task_id"], metadata["attempt_number"], metadata["status"]) == (
        str(case.run_id), str(case.task_id), 1, status,
    )
    return metadata


async def _result(client, body, case, status, *, preliminary=None, verdict=None, has_usage=False):
    projection = body(await client.get(_url(case, "/result")))
    usage = projection["usage"]
    if preliminary or has_usage:
        assert set(usage) == {"known_total_tokens", "usage_complete", "known_cost_usd", "cost_complete"}
    else:
        assert usage is None
    assert projection == {
        "run_id": str(case.run_id), "run_status": status, "preliminary_status": preliminary,
        "verification_verdict": verdict, "execution_failure_code": None, "usage": usage,
    }


async def _events(client, body, case, *, after=0):
    collected = []
    for _ in range(20):
        page = body(await client.get(_url(case, "/events"), params={"after_sequence": after, "limit": 2}))
        items = page["items"]
        assert [item["sequence"] for item in items] == list(range(after + 1, after + len(items) + 1))
        assert page["next_after_sequence"] == after + len(items)
        collected.extend(items)
        after = page["next_after_sequence"]
        if not page["has_more"]:
            return collected
        assert len(items) == 2
    pytest.fail("Event cursor did not reach the end of the fixture history")


def _assert_events(events, kinds, transitions):
    assert [event["kind"] for event in events] == list(kinds)
    assert [event["details"] for event in events if event["kind"] == TRANSITION] == [
        {"from": before, "to": after} for before, after in transitions
    ]
    for event in events:
        assert set(event) == {"sequence", "kind", "created_at", "details"}
        if event["kind"] == "agent_result_recorded":
            assert event["details"] == {"preliminary_status": "ready_for_verification"}
        if event["kind"].startswith(("approval_", "tool_call_")) or event["kind"] == "agent_usage_recorded":
            assert event["details"] == {}


def _assert_committed(case, state, events, status):
    run, task, _, persisted, _ = state
    assert (run.id, run.task_id, run.attempt_number, run.status) == (case.run_id, case.task_id, 1, status)
    assert (task.id, task.workspace_id, task.status, task.acceptance_criteria) == (
        case.task_id, case.binding.workspace_id, "queued", list(CRITERIA),
    )
    assert [(e["sequence"], e["kind"]) for e in events] == [(e.sequence, e.event_type) for e in persisted]


def _assert_safe(recorded, caplog, case, state, *markers):
    _, _, calls, _, approvals = state
    captured = "".join(recorded) + caplog.text
    for marker in (API_KEY, CONTENT, FILE, CALL_ID, READ_ID, WRITE_ID, str(case.binding.root), ANALYSIS.summary,
                   *(str(call.id) for call in calls), *(str(approval.id) for approval in approvals), *markers):
        assert marker not in captured
        assert json.dumps(marker, ensure_ascii=True)[1:-1] not in captured


async def _start(client, body, case):
    response = await _command(client, case.run_id, "start")
    assert body(response, 202) == {"run_id": str(case.run_id), "accepted": True}
    assert response.headers["Location"] == _url(case)


async def _verify(client, body, case, issued, events, transitions, verdict):
    plan, access = issued
    attempt = uuid4()
    service = VerificationService(case.factory)
    observed = await service.verify(plan, access, attempt_id=attempt, isolated_fixture=True)
    assert observed.verdict is verdict
    assert await service.verify(plan, access, attempt_id=attempt, isolated_fixture=True) == observed

    status = "succeeded" if verdict is VerificationVerdict.PASS else "failed"
    await _metadata(client, body, case, status)
    await _result(client, body, case, status, preliminary="ready_for_verification", verdict=verdict.value)
    tail = await _events(client, body, case, after=events[-1]["sequence"])
    _assert_events(tail, ("verification_recorded", TRANSITION), [("verifying", status)])
    assert tail[0]["details"] == {"verdict": verdict.value}
    assert await _events(client, body, case, after=tail[-1]["sequence"]) == []

    state = await _fresh_state(case)
    _assert_committed(case, state, events + tail, status)
    preliminary = decode_agent_result(state[0].final_result)
    assert preliminary.summary == ANALYSIS.summary
    assert _assert_finalized(
        state, preliminary, plan, attempt, verdict, [*transitions, ("verifying", status)],
    ) == observed
    return state


@pytest.mark.parametrize("outcome", ["preliminary", "pass", "fail"])
def test_http_11d_read_only_workflow(http_run, caplog, outcome):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(index, payload):
            if index == 0:
                entered.set()
                await release.wait()
                return _read_reply()
            if index == 1:
                assert payload["messages"][-1]["tool_call_id"] == CALL_ID
                return "stop", {"content": "Fixture inspected"}
            assert index == 2 and payload["response_format"]["type"] == "json_schema"
            return "stop", {"content": ANALYSIS.model_dump_json()}

        async with _offline_model(reply) as (model, calls), http_run(
            lambda case: RepositoryAgentRunner(case.factory, model, case.binding),
        ) as (app, client, case, body, runners, recorded):
            target = case.binding.root / FILE
            target.write_bytes(CONTENT.encode())
            expected = b"predeclared different fixture bytes" if outcome == "fail" else CONTENT.encode()
            issued = _issued_plan(case, [(FILE, expected)])
            assert not calls
            await _start(client, body, case)
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                task = _execution(app)
                assert not task.done() and len(calls) == 1
                await _metadata(client, body, case, "running")
                await _result(client, body, case, "running")
                running = await _events(client, body, case)
                _assert_events(running, (TRANSITION,), [("queued", "running")])
            finally:
                release.set()

            await _wait_for_cleanup(app, task)
            await _metadata(client, body, case, "verifying")
            await _result(client, body, case, "verifying", preliminary="ready_for_verification")
            events = running + await _events(client, body, case, after=running[-1]["sequence"])
            transitions = [("queued", "running"), ("running", "verifying")]
            _assert_events(events, READ_EVENTS + PRELIMINARY_EVENTS, transitions)
            state = await _fresh_state(case)
            _assert_committed(case, state, events, "verifying")
            assert decode_agent_result(state[0].final_result).summary == ANALYSIS.summary
            assert not state[4]
            _assert_audit(state[2][0], state[3], ("proposed", "running", "succeeded"))
            assert len(state[2]) == 1
            if outcome != "preliminary":
                state = await _verify(client, body, case, issued, events, transitions, VerificationVerdict(outcome))
            plan, _ = issued
            _assert_safe(recorded, caplog, case, state, str(plan.plan_id), plan.fingerprint,
                         plan.checks[0].expected_sha256)
            assert len(calls) == 3 and len(runners) == 1
            assert list(case.binding.root.iterdir()) == [target] and target.read_bytes() == CONTENT.encode()

    asyncio.run(check())


@pytest.mark.parametrize("decision", ["approve", "cancel"])
def test_http_11d_approved_write_hybrid(http_run, monkeypatch, caplog, decision):
    async def check():
        published, written, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        publish = PendingApprovals.publish

        def observe(pending, operation):
            publish(pending, operation)
            published.set()

        monkeypatch.setattr(PendingApprovals, "publish", observe)
        replacements = _record_replacements(monkeypatch, {FILE})

        async def reply(index, payload):
            if index == 0:
                return _tool_reply("read_file", READ_ID, path=FILE)
            if index == 1:
                assert payload["messages"][-1]["tool_call_id"] == READ_ID
                return _tool_reply("apply_patch", WRITE_ID, patch=patch)
            if index == 2:
                assert payload["messages"][-1]["tool_call_id"] == WRITE_ID
                written.set()
                await release.wait()
                return "stop", {"content": "Approved operation reported"}
            assert index == 3 and payload["response_format"]["type"] == "json_schema"
            return "stop", {"content": ANALYSIS.model_dump_json()}

        async with _offline_model(reply) as (model, calls), http_run(
            lambda case: _runner(case, model, scopes=(PathScope(FILE),)),
        ) as (app, client, case, body, runners, recorded):
            patch = _fixture_patch(case)
            target = case.binding.root / FILE
            issued = _issued_plan(case, [(FILE, APPROVED_BYTES)])
            assert not calls
            await _start(client, body, case)
            [runner] = runners
            task = _execution(app)
            try:
                preview = await _wait_for_preview(runner, task, published)
                assert preview.patch == patch and not replacements and target.read_bytes() == ORIGINAL.encode()
                await _metadata(client, body, case, "waiting_approval")
                await _result(client, body, case, "waiting_approval")
                pending = await _events(client, body, case)
                transitions = [("queued", "running"), ("running", "waiting_approval")]
                _assert_events(pending, APPROVAL_EVENTS, transitions)
                before = await _fresh_state(case)
                _assert_committed(case, before, pending, "waiting_approval")
                [waiting] = before[4]
                assert (waiting.id, waiting.status, waiting.decided_at) == (
                    preview.reference.approval_id, "pending", None,
                )
                assert {call.tool_name: call.status for call in before[2]} == {
                    "read_file": "succeeded", "apply_patch": "proposed",
                }
                if decision == "cancel":
                    response = body(await _command(client, case.run_id, "cancel"))
                    assert response == await _metadata(client, body, case, "cancelled")
                else:
                    # Test-only caller of the existing trusted approval bridge.
                    await runner._decide(preview, ApprovalDecision.APPROVE)
                    await asyncio.wait_for(written.wait(), timeout=5)
                    await _metadata(client, body, case, "running")
                    await _result(client, body, case, "running")
                    assert target.read_bytes() == APPROVED_BYTES and replacements == [FILE]
            finally:
                release.set()

            await _wait_for_cleanup(app, task)
            assert runner._approval_preview() is None
            if decision == "cancel":
                async with case.factory() as session:
                    with pytest.raises(StaleAgentRunStatusError):
                        await AgentRunService(session).complete_execution(case.run_id, FINAL)
                await _metadata(client, body, case, "cancelled")
            events = pending + await _events(client, body, case, after=pending[-1]["sequence"])
            state = await _fresh_state(case)
            run, _, tool_calls, durable_events, (approval,) = state
            assert len(tool_calls) == 2
            by_name = {call.tool_name: call for call in tool_calls}
            assert by_name["read_file"].status == "succeeded"
            write = by_name["apply_patch"]
            assert approval.id == preview.reference.approval_id and approval.tool_call_id == write.id
            assert approval.decided_at is not None
            audit = _write_events(durable_events, write)
            [decided] = [event for event in durable_events if event.event_type == "approval_decided"]
            expected_decision = {
                "schema_version": 1, "approval_id": str(approval.id), "tool_call_id": str(write.id),
                "decision": approval.status,
            }
            if decision == "cancel":
                _assert_events(
                    events, (*APPROVAL_EVENTS, TRANSITION, "approval_decided", "tool_call_finished", "agent_usage_recorded"),
                    [*transitions, ("waiting_approval", "cancelled")],
                )
                _assert_committed(case, state, events, "cancelled")
                await _result(client, body, case, "cancelled", has_usage=True)
                assert (write.status, approval.status) == ("failed", "rejected")
                assert [e.payload["status"] for e in audit] == ["proposed", "failed"]
                assert audit[-1].payload["error_code"] == "cancelled"
                assert decided.payload == {**expected_decision, "error_code": "cancelled"}
                assert run.final_result is None and not written.is_set()
                assert len(calls) == 2 and not replacements and target.read_bytes() == ORIGINAL.encode()
            else:
                transitions += [("waiting_approval", "running"), ("running", "verifying")]
                _assert_events(
                    events, (*APPROVAL_EVENTS, "approval_decided", TRANSITION, "tool_call_started",
                             "tool_call_finished", *PRELIMINARY_EVENTS), transitions,
                )
                _assert_committed(case, state, events, "verifying")
                await _metadata(client, body, case, "verifying")
                await _result(client, body, case, "verifying", preliminary="ready_for_verification")
                assert (write.status, approval.status) == ("succeeded", "approved")
                assert [e.payload["status"] for e in audit] == ["proposed", "running", "succeeded"]
                assert decided.payload == expected_decision
                state = await _verify(client, body, case, issued, events, transitions, VerificationVerdict.PASS)
                assert len(calls) == 4 and replacements == [FILE] and target.read_bytes() == APPROVED_BYTES
            plan, _ = issued
            _assert_safe(recorded, caplog, case, state, patch, REPLACEMENT, ORIGINAL, str(plan.plan_id),
                         plan.fingerprint, plan.checks[0].expected_sha256)
            assert list(case.binding.root.iterdir()) == [target]

    asyncio.run(check())
