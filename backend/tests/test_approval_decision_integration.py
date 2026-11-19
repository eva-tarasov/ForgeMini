"""HTTP approval decisions with real PostgreSQL and an offline approved LangGraph."""

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text, update
from test_agent_observability import agent_log_capture
from test_agent_runs_start_cancel_integration import _client, _command, _execution, _wait_for_cleanup
from test_approval_decision_api import DECISION_PATH, DIGEST, decision_url
from test_approval_details_api import detail_url
from test_approval_integration import database as approval_database
from test_approval_integration import snapshot
from test_approval_review_api import REVIEW_AUTH, REVIEW_TOKEN, review_url
from test_approvals_api import AUTH, TOKEN, http_logs
from test_approvals_api_integration import _add_approvals, _other_run
from test_approved_repository_integration import (
    ORIGINAL, READ_ID, WRITE_ID, _fixture_patch, _runner, _tool_reply, _wait_for_preview, _write_events,
)
from test_repository_agent_integration import API_KEY, FILE, _offline_model, database
from test_verification_e2e_integration import (
    ANALYSIS, APPROVED_BYTES, _assert_finalized, _assert_no_verification, _fresh_state, _issued_plan,
    _record_replacements, fixture_run,
)

from app.core.logging import JsonFormatter
from app.db.models import AgentEventModel
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.services import in_process_run_coordinator
from app.services.approval_gate import ApprovalGate
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallErrorCode
from app.services.verification_contract import VerificationVerdict
from app.services.verification_service import VerificationService


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None, reason="requires the dedicated repository DB",
)
REJECT = {"decision": "reject"}


def _access(workspace_id):
    assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
    return LocalApprovalAccessBinding(workspace_id, SecretStr(TOKEN), review_token=SecretStr(REVIEW_TOKEN))


def _conflict(response):
    assert response.status_code == 409
    assert response.json() == {"error": {
        "code": "APPROVAL_DECISION_CONFLICT", "message": "Approval decision conflicts with current state", "details": [],
    }}


def _recorded(response, flow, status):
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "approval_id": str(flow.reference.approval_id), "run_id": str(flow.reference.ticket.run_id),
        "decision": status, "recorded": True,
    }


def _decision_events(state):
    return [event.payload for event in state[3] if event.event_type == "approval_decided"]


def _assert_outcome(state, flow, *, run, approval, call, error=None):
    actual, task, calls, events, approvals = state
    assert (actual.status, task.status) == (run, "queued")
    [write] = [tool for tool in calls if tool.tool_name == "apply_patch"]
    [stored] = approvals
    assert (write.id, write.status) == (flow.reference.ticket.tool_call_id, call)
    assert (stored.id, stored.status, stored.tool_call_id) == (flow.reference.approval_id, approval, write.id)
    assert stored.decided_at is not None
    assert _write_events(events, write)[-1].payload.get("error_code") == error
    [decision] = _decision_events(state)
    assert (decision["approval_id"], decision["decision"]) == (str(stored.id), approval)


@asynccontextmanager
async def _http_pending(case, monkeypatch, *, verification_flow=False):
    patch = _fixture_patch(case)
    target = case.binding.root / FILE
    published = asyncio.Event()
    publish = PendingApprovals.publish

    def observe(pending, operation):
        publish(pending, operation)
        published.set()

    async def reply(index, payload):
        if not verification_flow:
            assert index == 0, "Only the patch proposal should reach the model"
            return _tool_reply("apply_patch", WRITE_ID, patch=patch)
        if index == 0:
            return _tool_reply("read_file", READ_ID, path=FILE)
        if index == 1:
            assert payload["messages"][-1]["tool_call_id"] == READ_ID
            return _tool_reply("apply_patch", WRITE_ID, patch=patch)
        if index == 2:
            assert payload["messages"][-1]["tool_call_id"] == WRITE_ID
            return "stop", {"content": "Operation reported"}
        assert index == 3 and payload["response_format"]["type"] == "json_schema"
        return "stop", {"content": ANALYSIS.model_dump_json()}

    with monkeypatch.context() as hooks:
        hooks.setattr(PendingApprovals, "publish", observe)
        replacements = _record_replacements(hooks, {FILE})
        async with _offline_model(reply) as (model, calls):
            runner = _runner(case, model, wait_seconds=8)
            app = create_app(
                session_factory=case.factory, approval_access_binding=_access(case.binding.workspace_id),
                execution_binding=RunExecutionBinding(case.binding.workspace_id, lambda *_: runner),
            )
            async with _client(app) as client:
                started = await _command(client, case.run_id, "start")
                assert started.status_code == 202
                task = _execution(app)
                reference = (await _wait_for_preview(runner, task, published)).reference
                assert target.read_text() == ORIGINAL and not replacements
                yield SimpleNamespace(
                    client=client, app=app, task=task, start_response=started, reference=reference,
                    target=target, patch=patch, calls=calls,
                    replacements=replacements, url=decision_url(case.run_id, reference.approval_id),
                    details=detail_url(case.run_id, reference.approval_id),
                    review=review_url(case.run_id, reference.approval_id),
                )


async def _reviewed_body(flow):
    response = await flow.client.get(flow.review, headers=REVIEW_AUTH)
    assert response.status_code == 200 and response.json()["patch"] == flow.patch
    assert response.json()["patch_sha256"] == flow.reference.patch_sha256
    return {"decision": "approve", "reviewed_patch_sha256": response.json()["patch_sha256"]}


async def _post(flow, body):
    return await flow.client.post(flow.url, headers=REVIEW_AUTH, json=body)


@pytest.mark.skipif(os.getenv("FORGE_APPROVAL_TEST_DB") is None, reason="requires the dedicated approval DB")
def test_postgres_decision_scopes_exact_ids_and_never_creates_a_runner(approval_database):
    async def check(case):
        run_id, workspace = case.context.run_id, case.context.workspace_id
        async with case.factory.begin() as session:
            [approval_id] = await _add_approvals(session, run_id, ["pending"])
        before = await snapshot(case, fresh=True)

        def unexpected(*_args):
            pytest.fail("Decision must only use an already running coordinator")

        app = create_app(
            session_factory=case.factory, approval_access_binding=_access(workspace),
            execution_binding=RunExecutionBinding(workspace, unexpected),
        )
        async with (
            _other_run(case) as (_, foreign, foreign_approval),
            _other_run(case, same_workspace=True) as (_, sibling, sibling_approval),
            _client(app) as client,
        ):
            for run, approval in (
                (run_id, uuid4()), (uuid4(), approval_id), (foreign, foreign_approval),
                (sibling, approval_id), (run_id, sibling_approval),
            ):
                response = await client.post(decision_url(run, approval), headers=REVIEW_AUTH, json=REJECT)
                assert response.status_code == 404
                assert response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
            _conflict(await client.post(decision_url(run_id, approval_id), headers=REVIEW_AUTH, json=REJECT))
        assert await snapshot(case, fresh=True) == before

    approval_database(check, status=AgentRunStatus.WAITING_APPROVAL)


def test_postgres_http_concurrent_approve_records_once_then_executes_and_verifies(
    fixture_run, monkeypatch, http_logs, agent_log_capture,
):
    async def check(case, _idle):
        plan, access = _issued_plan(case, [(FILE, APPROVED_BYTES)])
        async with _http_pending(case, monkeypatch, verification_flow=True) as flow:
            body = await _reviewed_body(flow)
            decide, claim = ApprovalGate.decide, ApprovalGate.claim
            entered, both, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            arrivals = []

            async def race(gate, reference, decision, *, context):
                arrivals.append(reference.approval_id)
                if len(arrivals) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), timeout=5)
                await decide(gate, reference, decision, context=context)

            async def pause_claim(gate, operation):
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
                await claim(gate, operation)

            with monkeypatch.context() as barriers:
                barriers.setattr(ApprovalGate, "decide", race)
                barriers.setattr(ApprovalGate, "claim", pause_claim)
                try:
                    responses = await asyncio.wait_for(asyncio.gather(_post(flow, body), _post(flow, body)), timeout=6)
                    success, conflict = sorted(responses, key=lambda response: response.status_code)
                    _recorded(success, flow, "approved")
                    _conflict(conflict)
                    assert arrivals == [flow.reference.approval_id] * 2
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    overwrite = await _post(flow, REJECT)
                    _conflict(overwrite)
                    _assert_outcome(await _fresh_state(case), flow, run="waiting_approval", approval="approved",
                                    call="proposed")
                    assert not flow.replacements and flow.target.read_text() == ORIGINAL
                finally:
                    release.set()
                await _wait_for_cleanup(flow.app, flow.task)

            preliminary = flow.task.result()
            assert (await _fresh_state(case))[0].status == "verifying"
            assert flow.replacements == [FILE] and flow.target.read_bytes() == APPROVED_BYTES
            attempt = uuid4()
            evidence = await VerificationService(case.factory).verify(plan, access, attempt_id=attempt, isolated_fixture=True)
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="succeeded", approval="approved", call="succeeded")
            assert evidence == _assert_finalized(
                state, preliminary, plan, attempt, VerificationVerdict.PASS,
                [("queued", "running"), ("running", "waiting_approval"), ("waiting_approval", "running"),
                 ("running", "verifying"), ("verifying", "succeeded")],
                private=(flow.patch, str(case.binding.root)),
            )
            assert len(flow.calls) == 4 and flow.replacements == [FILE]
            decision_logs = [r for r in http_logs if r.http_fields["route"] == DECISION_PATH]
            assert sorted(r.http_fields["status_code"] for r in decision_logs) == [200, 409, 409]
            output = "".join(x.text for x in [*responses, overwrite])
            output += "".join(JsonFormatter().format(r) + repr(r.__dict__) for r in http_logs)
            output += agent_log_capture.getvalue()
            for value in (TOKEN, REVIEW_TOKEN, API_KEY, flow.patch, body["reviewed_patch_sha256"],
                          flow.reference.sources_sha256, str(case.binding.root)):
                assert value not in output
            assert TOKEN not in repr(flow.calls) and REVIEW_TOKEN not in repr(flow.calls)

    fixture_run(check)


def test_postgres_http_reject_is_recorded_once_without_writing(fixture_run, monkeypatch):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            _recorded(await _post(flow, REJECT), flow, "rejected")
            _conflict(await _post(flow, REJECT))
            await _wait_for_cleanup(flow.app, flow.task)
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="failed", approval="rejected", call="denied", error="approval_rejected")
            _assert_no_verification(state[3])
            assert len(flow.calls) == 1 and not flow.replacements and flow.target.read_text() == ORIGINAL

    fixture_run(check)


def test_postgres_wrong_fingerprint_and_corrupt_request_cannot_reach_gate(fixture_run, monkeypatch, http_logs):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            body = await _reviewed_body(flow)
            invalid = {"decision": "approve", "reviewed_patch_sha256": DIGEST}
            assert invalid != body
            original = await _fresh_state(case)
            [event] = [e for e in original[3] if e.event_type == "approval_requested"]
            change = update(AgentEventModel).where(AgentEventModel.id == event.id)
            unreachable = AsyncMock(side_effect=AssertionError("Gate must not receive invalid request"))
            with monkeypatch.context() as hooks:
                hooks.setattr(ApprovalGate, "decide", unreachable)
                wrong = await _post(flow, invalid)
                _conflict(wrong)
                async with case.factory.begin() as session:
                    await session.execute(change.values(payload={**event.payload, "patch_sha256": DIGEST}))
                try:
                    corrupted = await _post(flow, REJECT)
                    assert corrupted.status_code == 500 and corrupted.json()["error"]["code"] == "APPROVAL_DECISION_FAILED"
                finally:
                    async with case.factory.begin() as session:
                        await session.execute(change.values(payload=event.payload))
                unreachable.assert_not_awaited()
            after = await _fresh_state(case)
            assert not _decision_events(after) and after[4][0].status == "pending"
            assert [e.payload for e in after[3]] == [e.payload for e in original[3]]
            output = wrong.text + corrupted.text + "".join(JsonFormatter().format(r) for r in http_logs)
            assert DIGEST not in output and flow.patch not in output and REVIEW_TOKEN not in output
            assert not flow.replacements and flow.target.read_text() == ORIGINAL
            _recorded(await _post(flow, REJECT), flow, "rejected")
            await _wait_for_cleanup(flow.app, flow.task)

    fixture_run(check)


def test_postgres_gate_rollback_leaves_no_decision_or_partial_audit(fixture_run, monkeypatch):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            record = ApprovalGate._record_decision
            before = await _fresh_state(case)
            staged = asyncio.Event()

            async def fail_after_staging(gate, session, *args, **kwargs):
                await record(gate, session, *args, **kwargs)
                await session.flush()
                staged.set()
                await session.execute(text("SELECT 1 / 0"))

            with monkeypatch.context() as hooks:
                hooks.setattr(ApprovalGate, "_record_decision", fail_after_staging)
                failed = await _post(flow, REJECT)
            assert staged.is_set() and failed.status_code == 500
            assert failed.json()["error"]["code"] == "APPROVAL_DECISION_FAILED"
            assert "division by zero" not in failed.text
            after = await _fresh_state(case)
            assert (after[0].status, after[2][0].status, after[4][0].status, after[4][0].decided_at) == (
                "waiting_approval", "proposed", "pending", None,
            )
            assert [(e.id, e.payload) for e in after[3]] == [(e.id, e.payload) for e in before[3]]
            assert not flow.replacements and flow.target.read_text() == ORIGINAL
            _recorded(await _post(flow, REJECT), flow, "rejected")
            await _wait_for_cleanup(flow.app, flow.task)
            assert len(_decision_events(await _fresh_state(case))) == 1

    fixture_run(check)


@pytest.mark.parametrize("interruption", ["cancel", "timeout", "shutdown"])
def test_postgres_http_decision_fails_closed_when_lifecycle_ends(fixture_run, monkeypatch, interruption):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            body = await _reviewed_body(flow)
            entered, release = asyncio.Event(), asyncio.Event()
            if interruption == "cancel":
                decide = ApprovalGate.decide

                async def pause(gate, reference, decision, *, context):
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                    await decide(gate, reference, decision, context=context)

                with monkeypatch.context() as hooks:
                    hooks.setattr(ApprovalGate, "decide", pause)
                    response = asyncio.create_task(_post(flow, body))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=3)
                        cancelled = await _command(flow.client, case.run_id, "cancel")
                        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                    finally:
                        release.set()
                    _conflict(await asyncio.wait_for(response, timeout=5))
            elif interruption == "timeout":
                finish = ApprovalGate.finish

                async def pause(gate, operation, *, error, duration_ms):
                    assert error is ToolCallErrorCode.APPROVAL_TIMEOUT
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                    await finish(gate, operation, error=error, duration_ms=duration_ms)

                loop = asyncio.get_running_loop()
                clock = loop.time
                with monkeypatch.context() as hooks:
                    hooks.setattr(ApprovalGate, "finish", pause)
                    hooks.setattr(loop, "time", lambda: clock() + 9)
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=3)
                        assert (await _fresh_state(case))[4][0].status == "pending"
                        _conflict(await _post(flow, body))
                    finally:
                        release.set()
                    await _wait_for_cleanup(flow.app, flow.task)
            else:
                cancel = in_process_run_coordinator.cancel_agent_run

                async def pause(*args):
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                    await cancel(*args)

                with monkeypatch.context() as hooks:
                    hooks.setattr(in_process_run_coordinator, "cancel_agent_run", pause)
                    shutdown = asyncio.create_task(flow.app.state.run_coordinator.close())
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=3)
                        assert (await _fresh_state(case))[4][0].status == "pending"
                        _conflict(await _post(flow, body))
                    finally:
                        release.set()
                    await asyncio.wait_for(shutdown, timeout=5)

            await _wait_for_cleanup(flow.app, flow.task)
            state = await _fresh_state(case)
            if interruption == "timeout":
                _assert_outcome(state, flow, run="failed", approval="rejected", call="failed", error="approval_timeout")
            else:
                _assert_outcome(state, flow, run="cancelled", approval="rejected", call="failed", error="cancelled")
            _assert_no_verification(state[3])
            assert not flow.replacements and flow.target.read_text() == ORIGINAL

    fixture_run(check)


def test_postgres_lost_response_reconciles_committed_approval_before_cancellation(fixture_run, monkeypatch):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            body = await _reviewed_body(flow)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def pause_claim(*_args):
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)

            async def interrupt(scope, receive, send):
                async def lost(message):
                    assert message["type"] == "http.response.start" and message["status"] == 200
                    raise ConnectionError("synthetic response loss")

                await flow.app(scope, receive, lost)

            with monkeypatch.context() as hooks:
                hooks.setattr(ApprovalGate, "claim", pause_claim)
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=interrupt), base_url="http://forge-tests.invalid",
                    ) as broken_client:
                        with pytest.raises(ConnectionError, match="synthetic response loss"):
                            await broken_client.post(flow.url, headers=REVIEW_AUTH, json=body)
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    reconciled = await flow.client.get(flow.details, headers=AUTH)
                    assert reconciled.status_code == 200
                    assert reconciled.json()["status"] == "approved" and reconciled.json()["decided_at"]
                    _conflict(await _post(flow, body))
                    assert not flow.replacements and flow.target.read_text() == ORIGINAL
                    cancelled = await _command(flow.client, case.run_id, "cancel")
                    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                    await _wait_for_cleanup(flow.app, flow.task)
                finally:
                    release.set()
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="cancelled", approval="approved", call="failed", error="cancelled")
            _assert_no_verification(state[3])
            assert not flow.replacements and flow.target.read_text() == ORIGINAL

    fixture_run(check)


def test_postgres_reviewed_approval_still_rechecks_changed_source_before_write(fixture_run, monkeypatch):
    async def check(case, _idle):
        async with _http_pending(case, monkeypatch) as flow:
            body = await _reviewed_body(flow)
            changed = "before\nsynthetic external edit outside hunk\n"
            flow.target.write_text(changed)
            _recorded(await _post(flow, body), flow, "approved")
            await _wait_for_cleanup(flow.app, flow.task)
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="failed", approval="approved", call="failed", error="approval_source_changed")
            _assert_no_verification(state[3])
            assert not flow.replacements and flow.target.read_text() == changed

    fixture_run(check)
