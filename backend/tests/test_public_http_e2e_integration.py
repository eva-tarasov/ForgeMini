"""Cross-API checks for approval, execution and independent verification."""

import asyncio
import json
import os
from contextlib import contextmanager
from functools import partial
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from test_agent_observability import agent_log_capture
from test_agent_runs_http_e2e_integration import _assert_committed, _events, _metadata, _result, _start
from test_agent_runs_start_cancel_integration import _client, _command, _execution, _wait_for_cleanup
from test_approval_decision_integration import REJECT, _access, _assert_outcome, _http_pending, _post
from test_approval_review_api import REVIEW_AUTH, REVIEW_TOKEN
from test_approvals_api import AUTH, TOKEN, http_logs
from test_approved_repository_integration import ORIGINAL, REPLACEMENT, _write_events
from test_repository_agent_integration import API_KEY, CALL_ID, CONTENT, FILE, _offline_model, _read_reply, database
from test_verification_e2e_integration import ANALYSIS, APPROVED_BYTES, _fresh_state, _issued_plan, fixture_run
from test_verification_evidence_api import evidence_url
from test_verification_evidence_api_integration import _persisted_signature

from app.core.logging import JsonFormatter
from app.main import create_app
from app.repositories.agent_events import AgentEventRepository
from app.services import verification_observer, verification_service
from app.services.agent_runs import StaleAgentRunStatusError
from app.services.approval_contracts import patch_fingerprint
from app.services.approval_gate import ApprovalGate
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.repository_agent_runner import RepositoryAgentRunner
from app.services.verification_contract import VerificationVerdict
from app.services.verification_evidence import VERIFICATION_RECORDED, decode_evidence
from app.services.verification_service import VerificationService
from app.tools.repository.filesystem import SafeRepositoryReader


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="requires the dedicated repository PostgreSQL database",
)


def _body(recorded, response, status=200, *, no_store=False):
    assert response.status_code == status, response.text
    if no_store:
        assert response.headers["cache-control"] == "no-store"
    recorded.append(response.text)
    return response.json()


def _assert_error(body, response, status, code):
    assert body(response, status)["error"]["code"] == code


def _recorded(body, response, case, flow, decision):
    assert body(response, no_store=True) == {
        "approval_id": str(flow.reference.approval_id), "run_id": str(case.run_id),
        "decision": decision, "recorded": True,
    }


def _evidence_summary(evidence, sequence):
    return {
        "attempt_id": str(evidence.attempt_id), "sequence": sequence, "verdict": evidence.verdict.value,
        "file_verdict": evidence.file_verdict.value, "audit_complete": evidence.audit_complete,
        "isolated_fixture": evidence.isolated_fixture, "check_count": len(evidence.plan.checks),
    }


async def _evidence_page(client, body, case, *, expected, after=0):
    payload = body(await client.get(evidence_url(case.run_id), headers=AUTH, params={
        "after_sequence": after, "limit": 20,
    }), no_store=True)
    assert payload == {
        "run_id": str(case.run_id), "items": expected,
        "next_after_sequence": expected[-1]["sequence"] if expected else after, "has_more": False,
    }


async def _evidence_detail(client, body, case, evidence, summary, outcome):
    [check] = evidence.plan.checks
    response = await client.get(evidence_url(case.run_id, evidence.attempt_id), headers=AUTH)
    assert body(response, no_store=True) == {
        **summary, "checks": [{
            "check_id": str(check.check_id), "criterion_index": check.criterion_index, "path": FILE, "outcome": outcome,
        }],
    }


def _assert_safe(recorded, http_logs, agent_log_capture, case, *private):
    formatter = JsonFormatter()
    logged = "".join(formatter.format(record) + repr(record.__dict__) for record in http_logs)
    assert "http_request_completed" in logged and "agent_operation_finished" in agent_log_capture.getvalue()
    output = logged + agent_log_capture.getvalue() + "".join(recorded)
    for marker in (TOKEN, REVIEW_TOKEN, API_KEY, str(case.binding.root), ANALYSIS.summary, *private):
        assert marker not in output and json.dumps(marker, ensure_ascii=False)[1:-1] not in output


@contextmanager
def _reads_cannot_execute(monkeypatch):
    with monkeypatch.context() as hooks:
        verify = AsyncMock(side_effect=AssertionError("GET must not reverify"))
        observe = AsyncMock(side_effect=AssertionError("GET must not observe"))
        read = Mock(side_effect=AssertionError("GET must not access a file"))
        hooks.setattr(VerificationService, "verify", verify)
        hooks.setattr(verification_service, "observe", observe)
        hooks.setattr(verification_observer, "observe", observe)
        hooks.setattr(SafeRepositoryReader, "read_text", read)
        try:
            yield
        finally:
            verify.assert_not_called()
            observe.assert_not_called()
            read.assert_not_called()


async def _discover_and_review(client, body, case, flow):
    row = await _fresh_state(case)
    [approval] = row[4]
    assert approval.status == "pending" and approval.decided_at is None
    assert (approval.id, approval.tool_call_id) == (flow.reference.approval_id, flow.reference.ticket.tool_call_id)
    listing = body(await client.get(f"/agent-runs/{case.run_id}/approvals", headers=AUTH))
    [item] = listing["items"]
    assert item == {
        "id": str(approval.id), "run_id": str(case.run_id), "tool_name": "apply_patch",
        "status": "pending", "requested_at": approval.requested_at.isoformat().replace("+00:00", "Z"),
    }
    detail = body(await client.get(flow.details, headers=AUTH))
    assert detail == {
        **item, "decided_at": None, "preview": {
            "state": "live_at_read", "content_included": False,
            "files": [{"path": FILE, "added_lines": 1, "removed_lines": 1, "hunk_count": 1}],
        },
    }
    # Exact review deliberately returns patch and patch fingerprint to the dedicated capability.
    review = await client.get(flow.review, headers=REVIEW_AUTH)
    assert review.status_code == 200 and review.headers["cache-control"] == "no-store"
    reviewed = review.json()
    assert reviewed == {
        "approval_id": str(approval.id), "run_id": str(case.run_id),
        "patch_sha256": flow.reference.patch_sha256, "patch": flow.patch,
    }
    assert patch_fingerprint(reviewed["patch"]) == reviewed["patch_sha256"]
    return reviewed["patch_sha256"]


def _assert_started(body, case, flow):
    assert body(flow.start_response, 202) == {"run_id": str(case.run_id), "accepted": True}
    assert flow.start_response.headers["location"] == f"/agent-runs/{case.run_id}"


def _assert_disposition_order(events, transitions):
    kinds = [event["kind"] for event in events]
    assert kinds.count("approval_requested") == kinds.count("approval_decided") == 1
    assert kinds.index("approval_requested") < kinds.index("approval_decided")
    assert [event["details"] for event in events if event["kind"] == "agent_run_status_changed"] == [
        {"from": before, "to": after} for before, after in transitions
    ]
    assert all(event["details"] == {} for event in events
               if event["kind"].startswith(("approval_", "tool_call_")))


def _state_signature(state):
    return (*_persisted_signature(state[:4]), [
        (approval.id, approval.tool_call_id, approval.status, approval.decided_at) for approval in state[4]
    ])


def test_http_12e_approve_to_verified_pass(fixture_run, monkeypatch, http_logs, agent_log_capture):
    async def check(case, idle):
        plan, access = _issued_plan(case, [(FILE, APPROVED_BYTES)])
        recorded = []
        body = partial(_body, recorded)
        async with _http_pending(case, monkeypatch, verification_flow=True) as flow:
            client = flow.client
            _assert_started(body, case, flow)
            await _metadata(client, body, case, "waiting_approval")
            fingerprint = await _discover_and_review(client, body, case, flow)
            approvals_url = f"/agent-runs/{case.run_id}/approvals"
            for headers in ({}, {"Authorization": "Bearer synthetic-wrong-read-capability"}):
                _assert_error(body, await client.get(approvals_url, headers=headers), 403, "APPROVAL_ACCESS_DENIED")
            _assert_error(body, await client.get(flow.review, headers=AUTH), 403, "APPROVAL_ACCESS_DENIED")
            _assert_error(body, await client.post(flow.url, headers=AUTH, json=REJECT), 403, "APPROVAL_ACCESS_DENIED")

            foreign_app = create_app(session_factory=case.factory, approval_access_binding=_access(uuid4()))
            async with _client(foreign_app) as outsider:
                _assert_error(body, await outsider.get(approvals_url, headers=AUTH), 404, "AGENT_RUN_NOT_FOUND")
                for url, headers in ((flow.details, AUTH), (flow.review, REVIEW_AUTH)):
                    _assert_error(body, await outsider.get(url, headers=headers), 404, "APPROVAL_NOT_FOUND")
                denied = await outsider.post(flow.url, headers=REVIEW_AUTH, json=REJECT)
                _assert_error(body, denied, 404, "APPROVAL_NOT_FOUND")

            claim, release_claim, at_claim = ApprovalGate.claim, asyncio.Event(), asyncio.Event()

            async def hold_claim(gate, operation):
                at_claim.set()
                await asyncio.wait_for(release_claim.wait(), timeout=5)
                await claim(gate, operation)

            approval_body = {"decision": "approve", "reviewed_patch_sha256": fingerprint}
            with monkeypatch.context() as hooks:
                hooks.setattr(ApprovalGate, "claim", hold_claim)
                try:
                    _recorded(body, await _post(flow, approval_body), case, flow, "approved")
                    await asyncio.wait_for(at_claim.wait(), timeout=3)
                    held = await _fresh_state(case)
                    _assert_outcome(held, flow, run="waiting_approval", approval="approved", call="proposed")
                    assert flow.target.read_text() == ORIGINAL and not flow.replacements
                finally:
                    release_claim.set()
                await _wait_for_cleanup(flow.app, flow.task)

            assert flow.task.result().summary == ANALYSIS.summary
            assert flow.replacements == [FILE] and flow.target.read_bytes() == APPROVED_BYTES
            await _metadata(client, body, case, "verifying")
            await _result(client, body, case, "verifying", preliminary="ready_for_verification")
            await _evidence_page(client, body, case, expected=[])
            preliminary_events = await _events(client, body, case)
            last = preliminary_events[-1]["sequence"]
            preliminary = await _fresh_state(case)
            _assert_outcome(preliminary, flow, run="verifying", approval="approved", call="succeeded")
            _assert_committed(case, preliminary, preliminary_events, "verifying")

            attempt_id = uuid4()
            staged, commit = asyncio.Event(), asyncio.Event()
            append = AgentEventRepository.append

            async def hold_terminal_event(repository, run_id, *, event_type, payload):
                event = await append(repository, run_id, event_type=event_type, payload=payload)
                if event_type == "agent_run_status_changed" and payload == {"from": "verifying", "to": "succeeded"}:
                    await repository.session.flush()
                    staged.set()
                    await asyncio.wait_for(commit.wait(), timeout=5)
                return event

            with monkeypatch.context() as hooks:
                hooks.setattr(AgentEventRepository, "append", hold_terminal_event)
                write = asyncio.create_task(VerificationService(case.factory).verify(
                    plan, access, attempt_id=attempt_id, isolated_fixture=True,
                ))
                try:
                    await asyncio.wait_for(staged.wait(), timeout=3)
                    await _metadata(client, body, case, "verifying")
                    await _result(client, body, case, "verifying", preliminary="ready_for_verification")
                    await _evidence_page(client, body, case, expected=[])
                    _assert_error(body, await client.get(evidence_url(case.run_id, attempt_id), headers=AUTH),
                                  404, "VERIFICATION_EVIDENCE_NOT_FOUND")
                    assert await _events(client, body, case, after=last) == []
                finally:
                    commit.set()
                    evidence = await asyncio.wait_for(write, timeout=5)

            assert evidence.verdict is VerificationVerdict.PASS
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="succeeded", approval="approved", call="succeeded")
            run, _, calls, durable_events, _ = state
            assert run.final_result == preliminary[0].final_result
            [tool] = [call for call in calls if call.tool_name == "apply_patch"]
            audit = _write_events(durable_events, tool)
            assert [event.payload["status"] for event in audit] == ["proposed", "running", "succeeded"]
            [decided] = [event for event in durable_events if event.event_type == "approval_decided"]
            [ready] = [event for event in durable_events if event.event_type == "agent_result_recorded"]
            [stored] = [event for event in durable_events if event.event_type == VERIFICATION_RECORDED]
            assert decided.sequence < audit[1].sequence < audit[2].sequence < ready.sequence < stored.sequence
            assert decode_evidence(stored.payload) == evidence
            summary = _evidence_summary(evidence, stored.sequence)
            with _reads_cannot_execute(monkeypatch):
                await _metadata(client, body, case, "succeeded")
                await _result(client, body, case, "succeeded", preliminary="ready_for_verification", verdict="pass")
                await _evidence_page(client, body, case, expected=[summary])
                await _evidence_detail(client, body, case, evidence, summary, "match")
                await _evidence_detail(client, body, case, evidence, summary, "match")
                await _evidence_page(client, body, case, expected=[], after=stored.sequence)
                tail = await _events(client, body, case, after=last)
                assert [(event["kind"], event["details"]) for event in tail] == [
                    (VERIFICATION_RECORDED, {"verdict": "pass"}),
                    ("agent_run_status_changed", {"from": "verifying", "to": "succeeded"}),
                ]
                public_events = preliminary_events + tail
                _assert_disposition_order(public_events, [
                    ("queued", "running"), ("running", "waiting_approval"), ("waiting_approval", "running"),
                    ("running", "verifying"), ("verifying", "succeeded"),
                ])
                _assert_committed(case, state, public_events, "succeeded")
                _assert_error(body, await client.get(evidence_url(case.run_id), headers=REVIEW_AUTH),
                              403, "APPROVAL_ACCESS_DENIED")
                for unknown in (evidence_url(uuid4()), evidence_url(case.run_id, uuid4())):
                    _assert_error(body, await client.get(unknown, headers=AUTH), 404, "VERIFICATION_EVIDENCE_NOT_FOUND")
                async with _client(foreign_app) as outsider:
                    _assert_error(body, await outsider.get(evidence_url(case.run_id, attempt_id), headers=AUTH),
                                  404, "VERIFICATION_EVIDENCE_NOT_FOUND")

            _assert_error(body, await _post(flow, approval_body), 409, "APPROVAL_DECISION_CONFLICT")
            _assert_error(body, await _command(client, case.run_id, "start"), 409, "RUN_ALREADY_STARTED")
            _assert_error(body, await _command(client, case.run_id, "cancel"), 409, "RUN_NOT_CANCELLABLE")
            assert _state_signature(await _fresh_state(case)) == _state_signature(state)
            assert len(flow.calls) == 4 and flow.replacements == [FILE] and flow.target.read_bytes() == APPROVED_BYTES
            _assert_safe(recorded, http_logs, agent_log_capture, case, flow.patch, ORIGINAL, REPLACEMENT,
                         fingerprint, flow.reference.sources_sha256, plan.fingerprint, *plan.criterion_digests,
                         plan.checks[0].expected_sha256, evidence.result_sha256, evidence.audit_sha256, evidence.access_sha256)
        idle()

    fixture_run(check)


def test_http_12e_reject_has_no_write_or_verification(fixture_run, monkeypatch, http_logs, agent_log_capture):
    async def check(case, idle):
        recorded = []
        body = partial(_body, recorded)
        async with _http_pending(case, monkeypatch) as flow:
            client = flow.client
            _assert_started(body, case, flow)
            fingerprint = await _discover_and_review(client, body, case, flow)
            _recorded(body, await _post(flow, REJECT), case, flow, "rejected")
            await _wait_for_cleanup(flow.app, flow.task)
            state = await _fresh_state(case)
            _assert_outcome(state, flow, run="failed", approval="rejected", call="denied", error="approval_rejected")
            [approval] = state[4]
            detail = body(await client.get(flow.details, headers=AUTH))
            assert detail["status"] == "rejected" and detail["decided_at"] == approval.decided_at.isoformat().replace("+00:00", "Z")
            assert detail["preview"] == {"state": "unavailable", "content_included": False, "files": []}
            assert body(await client.get(f"/agent-runs/{case.run_id}/approvals", headers=AUTH)) == {"items": []}
            _assert_error(body, await client.get(flow.review, headers=REVIEW_AUTH), 409, "APPROVAL_NOT_REVIEWABLE")
            for retry in (REJECT, {"decision": "approve", "reviewed_patch_sha256": fingerprint}):
                _assert_error(body, await _post(flow, retry), 409, "APPROVAL_DECISION_CONFLICT")
            await _metadata(client, body, case, "failed")
            result = body(await client.get(f"/agent-runs/{case.run_id}/result"))
            # The specific rejection reason belongs to the ToolCall audit checked above.
            assert (result["run_status"], result["preliminary_status"], result["verification_verdict"],
                    result["execution_failure_code"]) == ("failed", None, None, "graph_execution_failed")
            await _evidence_page(client, body, case, expected=[])
            events = await _events(client, body, case)
            _assert_disposition_order(events, [
                ("queued", "running"), ("running", "waiting_approval"), ("waiting_approval", "failed"),
            ])
            _assert_committed(case, state, events, "failed")
            assert {event["kind"] for event in events}.isdisjoint({VERIFICATION_RECORDED, "tool_call_started"})
            assert _state_signature(await _fresh_state(case)) == _state_signature(state)
            assert len(flow.calls) == 1 and not flow.replacements and flow.target.read_text() == ORIGINAL
            _assert_safe(recorded, http_logs, agent_log_capture, case, flow.patch, ORIGINAL, REPLACEMENT, fingerprint)
        idle()

    fixture_run(check)


@pytest.mark.parametrize("terminal", ["fail", "cancel"])
def test_http_12e_inconclusive_remains_visible_across_terminal_paths(
    fixture_run, monkeypatch, http_logs, agent_log_capture, terminal,
):
    async def check(case, idle):
        entered, resume = asyncio.Event(), asyncio.Event()
        recorded = []
        body = partial(_body, recorded)

        async def reply(index, payload):
            if index == 0:
                entered.set()
                await asyncio.wait_for(resume.wait(), timeout=5)
                return _read_reply()
            if index == 1:
                assert payload["messages"][-1]["tool_call_id"] == CALL_ID
                return "stop", {"content": "Fixture inspected"}
            assert index == 2 and payload["response_format"]["type"] == "json_schema"
            return "stop", {"content": ANALYSIS.model_dump_json()}

        async with _offline_model(reply) as (model, calls):
            app = create_app(
                session_factory=case.factory, approval_access_binding=_access(case.binding.workspace_id),
                execution_binding=RunExecutionBinding(
                    case.binding.workspace_id, lambda factory, _: RepositoryAgentRunner(factory, model, case.binding),
                ),
            )
            async with _client(app) as client:
                await _start(client, body, case)
                task = _execution(app)
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    assert not task.done()
                finally:
                    resume.set()
                await _wait_for_cleanup(app, task)

                plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
                verifier = VerificationService(case.factory)
                inconclusive = await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
                assert (inconclusive.file_verdict, inconclusive.verdict) == (
                    VerificationVerdict.PASS, VerificationVerdict.INCONCLUSIVE,
                )
                earlier = await _fresh_state(case)
                [first] = [event for event in earlier[3] if event.event_type == VERIFICATION_RECORDED]
                summary = _evidence_summary(inconclusive, first.sequence)
                await _metadata(client, body, case, "verifying")
                await _result(client, body, case, "verifying", preliminary="ready_for_verification", verdict="inconclusive")
                await _evidence_page(client, body, case, expected=[summary])
                previous_events = await _events(client, body, case)
                assert (previous_events[-1]["kind"], previous_events[-1]["details"]) == (
                    VERIFICATION_RECORDED, {"verdict": "inconclusive"},
                )

                late_plan, late_access = _issued_plan(case, [(FILE, b"expected bytes that differ from the fixture")])
                if terminal == "fail":
                    failed = await verifier.verify(late_plan, late_access, attempt_id=uuid4(), isolated_fixture=True)
                    assert failed.verdict is VerificationVerdict.FAIL
                    status, verdict, next_kinds = "failed", "fail", [VERIFICATION_RECORDED, "agent_run_status_changed"]
                else:
                    assert body(await _command(client, case.run_id, "cancel"))["status"] == "cancelled"
                    status, verdict, next_kinds = "cancelled", "inconclusive", ["agent_run_status_changed"]

                state = await _fresh_state(case)
                attempt_events = [event for event in state[3] if event.event_type == VERIFICATION_RECORDED]
                assert [decode_evidence(e.payload).attempt_id for e in attempt_events] == (
                    [inconclusive.attempt_id, failed.attempt_id] if terminal == "fail" else [inconclusive.attempt_id]
                )
                expected = [summary]
                if terminal == "fail":
                    expected.append(_evidence_summary(failed, attempt_events[-1].sequence))
                with _reads_cannot_execute(monkeypatch):
                    await _metadata(client, body, case, status)
                    await _result(client, body, case, status, preliminary="ready_for_verification", verdict=verdict)
                    await _evidence_page(client, body, case, expected=expected)
                    await _evidence_detail(client, body, case, inconclusive, summary, "match")
                    if terminal == "fail":
                        await _evidence_detail(client, body, case, failed, expected[-1], "mismatch")
                    tail = await _events(client, body, case, after=first.sequence)
                    assert [event["kind"] for event in tail] == next_kinds
                    if terminal == "fail":
                        assert tail[0]["details"] == {"verdict": "fail"}
                    assert tail[-1]["details"] == {"from": "verifying", "to": status}
                    _assert_committed(case, state, previous_events + tail, status)
                _assert_error(body, await _command(client, case.run_id, "start"), 409, "RUN_ALREADY_STARTED")
                with pytest.raises(StaleAgentRunStatusError):
                    await verifier.verify(late_plan, late_access, attempt_id=uuid4(), isolated_fixture=True)
                assert _state_signature(await _fresh_state(case)) == _state_signature(state)
                assert len(calls) == 3 and (case.binding.root / FILE).read_text() == CONTENT
                _assert_safe(recorded, http_logs, agent_log_capture, case, CONTENT, plan.fingerprint,
                             *plan.criterion_digests, late_plan.checks[0].expected_sha256, inconclusive.result_sha256)
        idle()

    fixture_run(check)
