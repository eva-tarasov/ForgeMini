"""Run the accepted repository graphs through independent verification on PostgreSQL."""

import json
import os
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.agent_runtime.structured_output import TaskAnalysis
from app.db.models import AgentEventModel, AgentRunModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope
from app.services.agent_result_codec import decode_agent_result
from app.services.agent_runner import AgentRunnerError
from app.services.agent_runs import StaleAgentRunStatusError
from app.services.approval_contracts import ApprovalDecision
from app.services.verification_contract import (
    FileDigestCheck,
    VerificationBinding,
    VerificationContractError,
    VerificationPlan,
    VerificationVerdict,
    aggregate_verification,
    fingerprint_criteria,
)
from app.services.verification_evidence import VERIFICATION_RECORDED, decode_evidence
from app.services.verification_observer import TrustedFixtureAccess
from app.services.verification_service import VerificationService
from app.tools.repository.filesystem import SafeRepositoryReader
from test_approved_repository_integration import (
    ORIGINAL,
    READ_ID,
    REPLACEMENT,
    WRITE_ID,
    _approval_state,
    _fixture_patch,
    _graph_failure,
    _run_agent,
    _tool_reply,
    _wait_for_preview,
    _write_events,
)
from test_repository_agent_integration import (
    API_KEY,
    CALL_ID,
    CONTENT,
    FILE,
    _assert_audit,
    _first_reply,
    _offline_model,
    _read_reply,
    database,
)


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="set FORGE_REPOSITORY_AGENT_TEST_DB and POSTGRES_DB to the dedicated fixture DB",
)

APPROVED_BYTES = f"{REPLACEMENT}\noutside hunk\n".encode()
CRITERIA = ("The designated fixture files contain the bytes specified by the trusted fixture plan",)
ANALYSIS = TaskAnalysis(
    summary="Execution reported a preliminary fixture result",
    relevant_components=["repository"],
    assumptions=["Independent verification is pending"],
    blocked_reason=None,
)


@pytest.fixture
def fixture_run(database):
    def run(scenario):
        async def seeded(case, idle):
            async with case.factory.begin() as session:
                task = await session.get(TaskModel, case.task_id)
                task.acceptance_criteria = list(CRITERIA)
            await scenario(case, idle)

        database(seeded)

    return run


def _issued_plan(case, files, *, binding=None):
    binding = binding or VerificationBinding(
        case.run_id, case.task_id, case.binding.workspace_id, case.binding.baseline.revision,
    )
    plan = VerificationPlan(
        uuid4(), binding, fingerprint_criteria(CRITERIA),
        tuple(FileDigestCheck(uuid4(), 0, path, sha256(data).hexdigest()) for path, data in files),
    )
    return plan, TrustedFixtureAccess(
        plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=binding,
        root=case.binding.root, read_scopes=tuple(PathScope(path) for path, _ in files),
        acceptance_criteria=CRITERIA,
    )


async def _execute_read(case, idle):
    async def reply(index, payload):
        idle()
        if index == 0:
            return _read_reply()
        if index == 1:
            message = payload["messages"][-1]
            assert message["tool_call_id"] == CALL_ID
            return "stop", {"content": "Fixture inspected"}
        assert index == 2 and payload["response_format"]["type"] == "json_schema"
        return "stop", {"content": ANALYSIS.model_dump_json()}

    async with _offline_model(reply) as (model, calls):
        service = case.service(model)
        result = await service.execute_run(case.run_id)
        assert len(calls) == 3
    return service, result


def _observe_reads(monkeypatch, idle):
    """Count physical reader calls while preserving the real filesystem implementation."""
    reads = []
    original = SafeRepositoryReader.read_text

    def record(reader, path, *, spec):
        idle()
        reads.append(path)
        return original(reader, path, spec=spec)

    monkeypatch.setattr(SafeRepositoryReader, "read_text", record)
    return reads


async def _fresh_state(case):
    engine = create_engine(case.settings)
    try:
        return await _approval_state(create_session_factory(engine), case)
    finally:
        await engine.dispose()


def _before_verification(state, preliminary):
    run, task, _, events, _ = state
    assert (run.status, task.status) == ("verifying", "queued")
    assert preliminary.status == "ready_for_verification"
    assert decode_agent_result(run.final_result) == preliminary
    assert not any(event.event_type == VERIFICATION_RECORDED for event in events)


def _assert_finalized(state, preliminary, plan, attempt, verdict, transitions, *, private=()):
    run, task, _, events, _ = state
    assert (run.status, task.status) == (transitions[-1][1], "queued")
    assert decode_agent_result(run.final_result) == preliminary
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    [result] = [event for event in events if event.event_type == "agent_result_recorded"]
    assert result.payload == {"schema_version": 1, "result_status": "ready_for_verification"}
    [recorded] = [event for event in events if event.event_type == VERIFICATION_RECORDED]
    assert result.sequence < recorded.sequence == events[-2].sequence
    assert events[-1].event_type == "agent_run_status_changed"
    status_events = [event.payload for event in events if event.event_type == "agent_run_status_changed"]
    assert status_events == [{"from": old, "to": new} for old, new in transitions]
    evidence = decode_evidence(recorded.payload)
    assert (evidence.attempt_id, evidence.plan, evidence.verdict) == (attempt, plan, verdict)
    assert evidence.audit_complete and evidence.isolated_fixture and evidence.file_verdict is verdict

    metadata = json.dumps([event.payload for event in events], ensure_ascii=False)
    for sentinel in (API_KEY, READ_ID, WRITE_ID, CALL_ID, *private):
        assert json.dumps(sentinel, ensure_ascii=False)[1:-1] not in metadata
    return evidence


def _assert_no_verification(events):
    assert not any(event.event_type in {"agent_result_recorded", VERIFICATION_RECORDED} for event in events)
    assert not any(
        event.event_type == "agent_run_status_changed" and event.payload["to"] in {"verifying", "succeeded"}
        for event in events
    )


@pytest.mark.parametrize("mismatch", [False, True], ids=["pass", "mismatch"])
def test_postgres_read_only_graph_reaches_durable_verdict(fixture_run, monkeypatch, mismatch):
    async def check(case, idle):
        plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
        attempt = uuid4()
        reads = _observe_reads(monkeypatch, idle)
        _, preliminary = await _execute_read(case, idle)
        _before_verification(await _fresh_state(case), preliminary)
        assert reads == [FILE]

        actual = b"controlled fixture mismatch" if mismatch else CONTENT.encode()
        if mismatch:
            (case.binding.root / FILE).write_bytes(actual)
        verification = VerificationService(case.factory)
        observed = await verification.verify(plan, access, attempt_id=attempt, isolated_fixture=True)
        state = await _fresh_state(case)
        _, _, (call,), events, approvals = state
        _assert_audit(call, events, ("proposed", "running", "succeeded"))
        assert not approvals
        expected = VerificationVerdict.FAIL if mismatch else VerificationVerdict.PASS
        terminal = "failed" if mismatch else "succeeded"
        durable = _assert_finalized(
            state, preliminary, plan, attempt, expected,
            [("queued", "running"), ("running", "verifying"), ("verifying", terminal)],
            private=(CONTENT, actual.decode(), str(case.binding.root)),
        )
        assert durable == observed
        [file_observation] = durable.observations.observations
        assert file_observation.observed_sha256 == sha256(actual).hexdigest()
        assert reads == [FILE, FILE]
        if not mismatch:
            assert await verification.verify(plan, access, attempt_id=attempt, isolated_fixture=True) == durable
            assert reads == [FILE, FILE]
            assert [event.id for event in (await _fresh_state(case))[3]] == [event.id for event in events]

    fixture_run(check)


def _record_replacements(monkeypatch, files, *, fail_on=None):
    attempts = []
    original = os.replace

    def record(source, target, **kwargs):
        if target in files:
            attempts.append(target)
            if target == fail_on:
                raise OSError("synthetic second replacement fault")
        return original(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", record)
    return attempts


def test_postgres_approved_read_write_graph_reaches_durable_success(fixture_run, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        target = case.binding.root / FILE
        plan, access = _issued_plan(case, [(FILE, APPROVED_BYTES)])
        attempt = uuid4()
        replacements = _record_replacements(monkeypatch, {FILE})

        async def reply(index, payload):
            idle()
            if index == 0:
                return _tool_reply("read_file", READ_ID, path=FILE)
            if index == 1:
                assert payload["messages"][-1]["tool_call_id"] == READ_ID
                return _tool_reply("apply_patch", WRITE_ID, patch=patch)
            if index == 2:
                assert payload["messages"][-1]["tool_call_id"] == WRITE_ID
                return "stop", {"content": "Approved operation reported"}
            assert index == 3 and payload["response_format"]["type"] == "json_schema"
            return "stop", {"content": ANALYSIS.model_dump_json()}

        async with _offline_model(reply) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, execution, published):
                preview = await _wait_for_preview(runner, execution, published)
                pending, task, before_calls, before_events, (waiting,) = await _fresh_state(case)
                assert (pending.status, task.status, waiting.status) == ("waiting_approval", "queued", "pending")
                assert {call.tool_name: call.status for call in before_calls} == {
                    "read_file": "succeeded", "apply_patch": "proposed",
                }
                [requested] = [event for event in before_events if event.event_type == "approval_requested"]
                assert requested.payload == preview.reference.requested_payload()
                assert waiting.id == preview.reference.approval_id
                assert preview.patch == patch and target.read_bytes() == ORIGINAL.encode() and not replacements
                await runner._decide(preview, ApprovalDecision.APPROVE)
                preliminary = await execution
            assert len(calls) == 4

        assert replacements == [FILE] and target.read_bytes() == APPROVED_BYTES
        _before_verification(await _fresh_state(case), preliminary)
        reads = _observe_reads(monkeypatch, idle)
        observed = await VerificationService(case.factory).verify(plan, access, attempt_id=attempt, isolated_fixture=True)
        assert reads == [FILE] and replacements == [FILE] and target.read_bytes() == APPROVED_BYTES
        state = await _fresh_state(case)
        _, _, tool_calls, events, (approval,) = state
        assert len(tool_calls) == 2
        by_name = {call.tool_name: call for call in tool_calls}
        for call in tool_calls:
            assert call.status == "succeeded"
            assert [event.payload["status"] for event in _write_events(events, call)] == [
                "proposed", "running", "succeeded",
            ]
        write = by_name["apply_patch"]
        [decision] = [event for event in events if event.event_type == "approval_decided"]
        assert (approval.id, approval.tool_call_id, approval.status) == (waiting.id, write.id, "approved")
        assert approval.decided_at is not None
        assert decision.payload == {
            "schema_version": 1, "approval_id": str(approval.id), "tool_call_id": str(write.id), "decision": "approved",
        }
        assert requested.sequence < decision.sequence < _write_events(events, write)[1].sequence
        durable = _assert_finalized(
            state, preliminary, plan, attempt, VerificationVerdict.PASS,
            [("queued", "running"), ("running", "waiting_approval"), ("waiting_approval", "running"),
             ("running", "verifying"), ("verifying", "succeeded")],
            private=(ORIGINAL, APPROVED_BYTES.decode(), patch, REPLACEMENT, str(case.binding.root)),
        )
        assert durable == observed

    fixture_run(check)


async def _assert_failed_run_refuses_verification(case, plan, access):
    with pytest.raises(VerificationContractError, match="^verification_preliminary_result_required$"):
        await VerificationService(case.factory).verify(plan, access, attempt_id=uuid4(), isolated_fixture=True)
    run, task, calls, events, approvals = await _fresh_state(case)
    assert (run.status, run.final_result, task.status) == ("failed", None, "queued")
    _assert_no_verification(events)
    return calls, events, approvals


def test_postgres_denied_graph_never_reads_or_creates_verification(fixture_run, monkeypatch):
    async def check(case, idle):
        plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
        reads = _observe_reads(monkeypatch, idle)
        denied = case.binding.root / "denied.txt"
        async with _offline_model(_first_reply(idle, denied.name)) as (model, replies):
            with pytest.raises(AgentRunnerError, match="^graph_execution_failed$") as exc:
                await case.service(model).execute_run(case.run_id)
            assert exc.value.__cause__.args == ("permission_denied",)
            assert len(replies) == 1
        (call,), events, approvals = await _assert_failed_run_refuses_verification(case, plan, access)
        _assert_audit(call, events, ("proposed", "denied"), error="permission_denied")
        assert not reads and not approvals
        assert denied.read_bytes() == (case.binding.root / FILE).read_bytes() == CONTENT.encode()

    fixture_run(check)


@pytest.mark.parametrize("partial", [False, True], ids=["rejected", "partial-apply"])
def test_postgres_approval_failure_never_enters_verification(fixture_run, monkeypatch, partial):
    async def check(case, idle):
        paths = (FILE, "second.txt") if partial else (FILE,)
        patch = _fixture_patch(case, *paths)
        plan, access = _issued_plan(case, [(path, APPROVED_BYTES) for path in paths])
        attempts = _record_replacements(monkeypatch, set(paths), fail_on=paths[-1] if partial else None)
        code = "patch_partial_apply" if partial else "approval_rejected"
        decision = ApprovalDecision.APPROVE if partial else ApprovalDecision.REJECT

        async def reply(index, _payload):
            idle()
            assert index == 0
            return _tool_reply("apply_patch", WRITE_ID, patch=patch)

        async with _offline_model(reply) as (model, replies):
            async with _run_agent(case, model, monkeypatch) as (_, runner, execution, published):
                preview = await _wait_for_preview(runner, execution, published)
                assert not attempts
                assert all((case.binding.root / path).read_bytes() == ORIGINAL.encode() for path in paths)
                await runner._decide(preview, decision)
                await _graph_failure(execution, code)
            assert len(replies) == 1
        (call,), events, (approval,) = await _assert_failed_run_refuses_verification(case, plan, access)
        assert call.status == ("failed" if partial else "denied")
        assert approval.id == preview.reference.approval_id and approval.tool_call_id == call.id
        assert approval.status == ("approved" if partial else "rejected")
        assert approval.decided_at is not None
        [requested] = [event for event in events if event.event_type == "approval_requested"]
        [decided] = [event for event in events if event.event_type == "approval_decided"]
        assert requested.payload == preview.reference.requested_payload()
        assert decided.payload == {
            "schema_version": 1, "approval_id": str(approval.id), "tool_call_id": str(call.id),
            "decision": approval.status,
        }
        lifecycle = _write_events(events, call)
        assert [event.payload["status"] for event in lifecycle] == (
            ["proposed", "running", "failed"] if partial else ["proposed", "denied"]
        )
        assert lifecycle[-1].payload["error_code"] == code
        assert attempts == (list(paths) if partial else [])
        assert (case.binding.root / FILE).read_bytes() == (APPROVED_BYTES if partial else ORIGINAL.encode())
        assert (case.binding.root / paths[-1]).read_bytes() == ORIGINAL.encode()
        metadata = json.dumps([event.payload for event in events], ensure_ascii=False)
        for private in (patch, REPLACEMENT, "synthetic second replacement fault", str(case.binding.root)):
            assert json.dumps(private)[1:-1] not in metadata

    fixture_run(check)


def test_postgres_cancel_after_preliminary_blocks_late_observation(fixture_run, monkeypatch):
    async def check(case, idle):
        plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
        service, preliminary = await _execute_read(case, idle)
        _before_verification(await _fresh_state(case), preliminary)
        await service.cancel_run(case.run_id)
        reads = _observe_reads(monkeypatch, idle)
        before = await _fresh_state(case)
        with pytest.raises(StaleAgentRunStatusError) as exc:
            await VerificationService(case.factory).verify(plan, access, attempt_id=uuid4(), isolated_fixture=True)
        assert (exc.value.expected, exc.value.actual) == (AgentRunStatus.VERIFYING, AgentRunStatus.CANCELLED)
        run, task, _, events, _ = await _fresh_state(case)
        assert (run.status, task.status) == ("cancelled", "queued")
        assert decode_agent_result(run.final_result) == preliminary
        assert [event.id for event in events] == [event.id for event in before[3]]
        assert events[-1].payload == {"from": "verifying", "to": "cancelled"}
        assert not reads and not any(event.event_type == VERIFICATION_RECORDED for event in events)

    fixture_run(check)


def test_postgres_two_graph_runs_cannot_exchange_verification_binding(fixture_run, monkeypatch):
    async def check(first, idle):
        root = first.binding.root / "second-workspace"
        root.mkdir()
        (root / FILE).write_bytes(CONTENT.encode())
        second = replace(first, run_id=uuid4(), task_id=uuid4(), binding=replace(
            first.binding, root=root, workspace_id=uuid4(),
        ))
        async with first.factory.begin() as session:
            session.add(WorkspaceModel(id=second.binding.workspace_id, name="second disposable fixture"))
            await session.flush()
            session.add(TaskModel(
                id=second.task_id, workspace_id=second.binding.workspace_id,
                goal="Inspect a separate fixture workspace", acceptance_criteria=list(CRITERIA), status="queued",
            ))
            await session.flush()
            session.add(AgentRunModel(id=second.run_id, task_id=second.task_id, attempt_number=1, status="queued"))
        try:
            plan_a, access_a = _issued_plan(first, [(FILE, CONTENT.encode())])
            plan_b, access_b = _issued_plan(second, [(FILE, CONTENT.encode())])
            await _execute_read(first, idle)
            await _execute_read(second, idle)
            reads = _observe_reads(monkeypatch, idle)
            verifier = VerificationService(first.factory)
            stored = await verifier.verify(plan_a, access_a, attempt_id=uuid4(), isolated_fixture=True)
            assert aggregate_verification(plan_b, stored.observations) is VerificationVerdict.INCONCLUSIVE

            foreign_binding = replace(plan_b.binding, workspace_id=first.binding.workspace_id)
            foreign_plan, foreign_access = _issued_plan(second, [(FILE, CONTENT.encode())], binding=foreign_binding)
            for plan, access, reason in (
                (plan_b, access_a, "verification_access_mismatch"),
                (foreign_plan, foreign_access, "verification_sql_binding_mismatch"),
            ):
                with pytest.raises(VerificationContractError, match=f"^{reason}$"):
                    await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=True)
            run, task, _, events, _ = await _fresh_state(second)
            assert (run.status, task.status) == ("verifying", "queued")
            assert not any(event.event_type == VERIFICATION_RECORDED for event in events)
            assert reads == [FILE]
            assert (await _fresh_state(first))[0].status == "succeeded"
        finally:
            async with first.factory.begin() as session:
                await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == second.run_id))
                await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == second.run_id))
                await session.execute(delete(AgentRunModel).where(AgentRunModel.id == second.run_id))
                await session.execute(delete(TaskModel).where(TaskModel.id == second.task_id))
                await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == second.binding.workspace_id))

    fixture_run(check)
