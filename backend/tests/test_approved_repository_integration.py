"""Opt-in approval graph tests on the migrated, dedicated Week 4 PostgreSQL database."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from test_agent_observability import agent_log_capture
from test_approval_contracts import patch_for
from test_repository_agent_integration import FILE, _offline_model, _snapshot, database

from app.agent_runtime.structured_output import TaskAnalysis
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, ToolCallModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope
from app.repositories.agent_events import AgentEventRepository
from app.services import agent_service as agent_service_module, pending_approvals as pending_approvals_module
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunService, StaleAgentRunStatusError, VerificationEvidenceRequiredError
from app.services.agent_service import AgentService
from app.services.approval_contracts import ApprovalDecision, patch_fingerprint
from app.services.approval_gate import ApprovalGate
from app.services.approval_outcome import ApprovalOutcome, ApprovalOutcomeHolder, ApprovalOutcomePort
from app.services.approved_repository_agent_runner import ApprovedRepositoryAgentRunner, ApprovedRepositoryFixtureBinding
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallErrorCode
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.repository.writer import SafeRepositoryWriter


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="set FORGE_REPOSITORY_AGENT_TEST_DB and POSTGRES_DB to opt in to Step 09B",
)
ORIGINAL = "before\noutside hunk\n"
REPLACEMENT = "synthetic-approved-change"
READ_ID = "model-fixture-read"
WRITE_ID = "model-fixture-patch"


def _tool_reply(name, call_id, **arguments):
    return "tool_calls", {"content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def _patch_reply(patch, idle):
    async def reply(index, _payload):
        assert index == 0
        idle()
        return _tool_reply("apply_patch", WRITE_ID, patch=patch)
    return reply


def _runner(case, model, *, scopes=None, wait_seconds=5):
    binding = ApprovedRepositoryFixtureBinding(
        case.binding.workspace_id, case.binding.root, case.binding.baseline,
        frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE}),
        (PathScope(".", recursive=True),),
        scopes if scopes is not None else (PathScope(".", recursive=True),),
    )
    return ApprovedRepositoryAgentRunner(
        case.factory, model, binding,
        run_id=case.run_id, wait_seconds=wait_seconds,
    )


@asynccontextmanager
async def _run_agent(case, model, monkeypatch, *, scopes=None, wait_seconds=5, deadline=10, pricing=None):
    published = asyncio.Event()
    original = PendingApprovals.publish

    def publish(pending, operation):
        original(pending, operation)
        published.set()

    runner = _runner(case, model, scopes=scopes, wait_seconds=wait_seconds)
    service = AgentService(case.factory, runner, execution_timeout_seconds=deadline, pricing=pricing)
    with monkeypatch.context() as changes:
        changes.setattr(PendingApprovals, "publish", publish)
        task = asyncio.create_task(service.execute_run(case.run_id))
        try:
            yield service, runner, task, published
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert runner._approval_preview() is None
            assert case.run_id not in service._active_runs


async def _approval_state(factory, case):
    run, task, calls, events = await _snapshot(factory, case)
    async with factory() as session:
        approvals = (await session.scalars(select(ApprovalModel).where(
            ApprovalModel.tool_call_id.in_([call.id for call in calls])
        ))).all()
    return run, task, calls, events, approvals


async def _graph_failure(task, reason):
    with pytest.raises(AgentRunnerError, match="^graph_execution_failed$") as error:
        await task
    assert error.value.__cause__.args == (reason,)


async def _wait_for_preview(runner, task, published):
    waiter = asyncio.create_task(published.wait())
    try:
        async with asyncio.timeout(3):
            await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
            pytest.fail("Agent completed before publishing an approval")
        preview = runner._approval_preview()
        assert preview is not None
        return preview
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def test_postgres_approved_graph_rejection_preserves_original_failure(database, monkeypatch):
    async def check(case, idle):
        target = case.binding.root / FILE
        target.write_text(ORIGINAL)
        patch = patch_for(FILE, after=REPLACEMENT)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                assert preview.patch == patch
                await runner._decide(preview, ApprovalDecision.REJECT)
                run, _, (call,), _, (approval,) = await _approval_state(case.factory, case)
                assert (run.status, call.status, approval.status) == ("failed", "denied", "rejected")
                await _graph_failure(task, "approval_rejected")
        async with case.factory() as session:
            runs = AgentRunService(session)
            failure = AgentFailure(AgentFailureCode.GRAPH_EXECUTION_FAILED)
            for forged in (
                ApprovalOutcome(replace(preview.reference, approval_id=uuid4()), ToolCallErrorCode.APPROVAL_REJECTED),
                ApprovalOutcome(replace(preview.reference, patch_sha256="a" * 64), ToolCallErrorCode.APPROVAL_REJECTED),
                ApprovalOutcome(preview.reference, ToolCallErrorCode.HANDLER_FAILED),
            ):
                with pytest.raises(StaleAgentRunStatusError):
                    await runs.fail_execution(case.run_id, failure, approval_failure=forged)
            await runs.fail_execution(
                case.run_id, failure,
                approval_failure=ApprovalOutcome(preview.reference, ToolCallErrorCode.APPROVAL_REJECTED),
            )
        run, parent, (call,), events, (approval,) = await _approval_state(case.factory, case)
        assert (run.status, parent.status, call.status, approval.status) == ("failed", "queued", "denied", "rejected")
        assert run.final_result is None and target.read_text() == ORIGINAL and len(calls) == 1
        assert [event.payload["code"] for event in events if event.event_type == "agent_execution_failed"] == [
            "graph_execution_failed",
        ]

    database(check)


def test_postgres_approved_graph_refused_outcome_binding_leaves_no_records(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        async with case.factory() as session:
            request = await AgentRunService(session).claim_for_execution(case.run_id)
        holder = ApprovalOutcomeHolder(case.run_id)
        assert holder.take_and_close() is None
        # Если request всё же закоммитится, остановить граф после commit, не дожидаясь approval timeout.
        publish = Mock(side_effect=ToolExecutionError(ToolErrorCode.APPROVAL_INVALID))
        handler = AsyncMock()
        monkeypatch.setattr(PendingApprovals, "publish", publish)
        monkeypatch.setattr(ToolExecutor, "_invoke_handler", handler)

        reading = create_engine(case.settings)
        try:
            observer = create_session_factory(reading)
            _, _, _, before, _ = await _approval_state(observer, case)
            async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
                runner = _runner(case, model)
                await _graph_failure(runner.run_with_approval_outcome(request, outcome=holder.port()), "approval_invalid")
                assert runner._approval_preview() is None
            run, task, tool_calls, after, approvals = await _approval_state(observer, case)
            assert (len(tool_calls), len(approvals), len(after) - len(before)) == (0, 0, 0)
            assert (run.status, task.status) == ("running", "queued")
            assert [(event.id, event.payload) for event in after] == [(event.id, event.payload) for event in before]
        finally:
            await reading.dispose()
        assert len(calls) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
        publish.assert_not_called()
        handler.assert_not_awaited()

    database(check)


def _fixture_patch(case, *paths):
    paths = paths or (FILE,)
    for path in paths:
        (case.binding.root / path).write_text(ORIGINAL)
    return patch_for(*paths, after=REPLACEMENT)


def _write_events(events, call):
    audit = [
        event for event in events if event.event_type.startswith("tool_call_")
        and event.payload["tool_call_id"] == str(call.id)
    ]
    for event in audit:
        assert event.payload.keys() <= {
            "schema_version", "tool_call_id", "tool_name", "tool_version", "status", "error_code", "duration_ms",
        }
        assert event.payload["tool_name"] == call.tool_name and event.payload["tool_version"] == 1
    return audit


def _expect_write_outcome(state, *, run_status, call_status, approval_status, error):
    run, task, (call,), events, (approval,) = state
    assert (run.status, task.status, call.status, approval.status) == (
        run_status, "queued", call_status, approval_status,
    )
    assert approval.tool_call_id == call.id and run.final_result is None
    audit = _write_events(events, call)
    assert audit[-1].payload["status"] == call_status
    assert audit[-1].payload.get("error_code") == error
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    failures = [event for event in events if event.event_type == "agent_execution_failed"]
    assert len(failures) == (1 if run_status == "failed" else 0)
    return audit


@pytest.mark.parametrize("publication", ["duplicate", "foreign", "conflict"])
def test_postgres_approved_graph_outcome_guards_terminal_failure(database, monkeypatch, publication):
    async def check(case, idle):
        patch = _fixture_patch(case)
        original_port = ApprovalOutcomeHolder.port
        observed, accepted = [], []

        def port_for(holder):
            port = original_port(holder)

            def publish(outcome):
                observed.append(outcome)
                if publication == "foreign":
                    forged = replace(outcome.reference, sources_sha256="b" * 64)
                    accepted.append(port.publish(replace(outcome, reference=forged)))
                else:
                    accepted.append(port.publish(outcome))
                    repeated = replace(outcome) if publication == "duplicate" else replace(
                        outcome, code=ToolCallErrorCode.CANCELLED,
                    )
                    accepted.append(port.publish(repeated))
                    accepted.append(port.publish(outcome))
                return accepted[-1]

            return ApprovalOutcomePort(port.bind, publish)

        monkeypatch.setattr(ApprovalOutcomeHolder, "port", port_for)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                await runner._decide(preview, ApprovalDecision.REJECT)
                if publication == "duplicate":
                    await _graph_failure(task, "approval_rejected")
                else:
                    with pytest.raises(StaleAgentRunStatusError) as stale:
                        await task
                    assert isinstance(stale.value.__cause__, AgentRunnerError)
                    assert stale.value.__cause__.reason == "graph_execution_failed"
                    assert stale.value.__cause__.__cause__.args == ("approval_rejected",)
        assert observed == [ApprovalOutcome(preview.reference, ToolCallErrorCode.APPROVAL_REJECTED)]
        assert accepted == {"duplicate": [True, True, True], "foreign": [False], "conflict": [True, False, False]}[publication]
        reading = create_engine(case.settings)
        try:
            run, task, (call,), events, (approval,) = await _approval_state(create_session_factory(reading), case)
            assert (run.status, task.status, call.status, approval.status) == ("failed", "queued", "denied", "rejected")
            finished = [event.payload["error_code"] for event in events if event.event_type == "tool_call_finished"]
            failures = [event.payload["code"] for event in events if event.event_type == "agent_execution_failed"]
            assert finished == ["approval_rejected"]
            assert failures == (["graph_execution_failed"] if publication == "duplicate" else [])
            assert run.final_result is None
        finally:
            await reading.dispose()
        assert (case.binding.root / FILE).read_text() == ORIGINAL and len(calls) == 1

    database(check)


def test_postgres_approved_graph_reads_then_writes_and_persists_verifying(database, monkeypatch, agent_log_capture):
    async def check(case, idle):
        patch = _fixture_patch(case)
        target = case.binding.root / FILE
        reading = create_engine(case.settings)
        fresh = create_session_factory(reading)
        invocations = []
        original = ToolExecutor._invoke_handler
        real_claim = ApprovalGate.claim
        duplicate_claims = []

        async def check_claim(gate, operation):
            await real_claim(gate, operation)
            with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                await real_claim(gate, operation)
            duplicate_claims.append(operation.reference.ticket.tool_call_id)

        async def observe_handler(executor, tool, arguments, **kwargs):
            if tool.spec.name == "apply_patch":
                idle()
                run, _, calls, events, (approval,) = await _approval_state(fresh, case)
                (call,) = [call for call in calls if call.tool_name == "apply_patch"]
                assert (run.status, call.status, approval.status) == ("running", "running", "approved")
                assert approval.tool_call_id == call.id
                assert [event.payload["status"] for event in _write_events(events, call)] == ["proposed", "running"]
                decision = next(event for event in events if event.event_type == "approval_decided")
                assert decision.payload["decision"] == "approved"
                assert decision.sequence < _write_events(events, call)[-1].sequence
                assert target.read_text() == ORIGINAL
                invocations.append(call.id)
                idle()
            return await original(executor, tool, arguments, **kwargs)

        monkeypatch.setattr(ApprovalGate, "claim", check_claim)
        monkeypatch.setattr(ToolExecutor, "_invoke_handler", observe_handler)
        research = "The approved change was reported for a disposable fixture."
        analysis = TaskAnalysis(
            summary="Fixture updated after approval", relevant_components=["repository"],
            assumptions=["Independent verification is pending"], blocked_reason=None,
        )

        async def reply(index, payload):
            idle()
            if index == 0:
                assert payload["parallel_tool_calls"] is False
                assert [entry["function"]["name"] for entry in payload["tools"]] == [
                    "list_files", "read_file", "search_files", "git_diff", "apply_patch",
                ]
                return _tool_reply("read_file", READ_ID, path=FILE)
            if index == 1:
                message = payload["messages"][-1]
                assert message["role"] == "tool" and message["tool_call_id"] == READ_ID
                assert json.loads(message["content"])["content"] == ORIGINAL
                return _tool_reply("apply_patch", WRITE_ID, patch=patch)
            if index == 2:
                message = payload["messages"][-1]
                assert message["role"] == "tool" and message["tool_call_id"] == WRITE_ID
                assert json.loads(message["content"]) == {"changed_files": 1, "changed_paths": [FILE]}
                return "stop", {"content": research}
            assert index == 3 and payload["response_format"]["type"] == "json_schema"
            assert not payload.get("tools")
            prompt = json.loads(payload["messages"][-1]["content"])
            assert prompt["observations"] == [] and prompt["analysis_notes"] == [research]
            return "stop", {"content": analysis.model_dump_json()}

        tariff = ModelTariff("openai", "gpt-4o-mini", "fixture-v1", Decimal("1"), Decimal("2"))
        try:
            async with _offline_model(reply) as (model, replies):
                async with _run_agent(case, model, monkeypatch, pricing=AgentPricing(tariff, tariff)) as (
                    _, runner, task, published,
                ):
                    preview = await _wait_for_preview(runner, task, published)
                    idle()
                    run, parent, calls, events, (approval,) = await _approval_state(fresh, case)
                    assert (run.status, parent.status, approval.status) == ("waiting_approval", "queued", "pending")
                    assert target.read_text() == ORIGINAL and preview.patch == patch and patch not in repr(preview)
                    assert preview.reference.approval_id == approval.id
                    assert preview.reference.ticket.tool_call_id == approval.tool_call_id
                    assert preview.reference.patch_sha256 == patch_fingerprint(patch)
                    requested = next(event for event in events if event.event_type == "approval_requested")
                    assert requested.payload == preview.reference.requested_payload()
                    statuses = {call.tool_name: call.status for call in calls}
                    assert statuses == {"read_file": "succeeded", "apply_patch": "proposed"}
                    await runner._decide(preview, ApprovalDecision.APPROVE)
                    result = await task

                assert len(replies) == 4
                reader = AgentService(fresh, _runner(case, model))
                assert await reader.get_run_result(case.run_id) == result
                usage = await reader.get_run_usage(case.run_id)
                assert usage.snapshot.reported_calls == 4 and usage.snapshot.known_total_tokens == 120
                assert usage.cost_estimate.cost_complete and usage.cost_estimate.known_cost_usd == Decimal("0.00016")

            run, parent, calls, events, (approval,) = await _approval_state(fresh, case)
            assert (run.status, parent.status, approval.status) == ("verifying", "queued", "approved")
            assert result.summary == analysis.summary and result.observations == ()
            assert result.status == "ready_for_verification" and run.final_result["schema_version"] == 1
            assert target.read_text() == f"{REPLACEMENT}\noutside hunk\n"
            by_name = {call.tool_name: call for call in calls}
            assert len(calls) == 2
            assert invocations == duplicate_claims == [by_name["apply_patch"].id]
            assert all(isinstance(call.id, UUID) and str(call.id) not in (READ_ID, WRITE_ID) for call in calls)
            assert len({call.id for call in calls}) == 2
            for call in calls:
                assert [item.payload["status"] for item in _write_events(events, call)] == [
                    "proposed", "running", "succeeded",
                ]
            status_changes = [event.payload for event in events if event.event_type == "agent_run_status_changed"]
            assert [(change["from"], change["to"]) for change in status_changes] == [
                ("queued", "running"), ("running", "waiting_approval"),
                ("waiting_approval", "running"), ("running", "verifying"),
            ]
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            assert [event.event_type for event in events] == [
                "agent_run_status_changed", "tool_call_proposed", "tool_call_started", "tool_call_finished",
                "tool_call_proposed", "approval_requested", "agent_run_status_changed", "approval_decided",
                "agent_run_status_changed", "tool_call_started", "tool_call_finished", "agent_result_recorded",
                "agent_run_status_changed", "agent_usage_recorded",
            ]
            async with fresh() as session:
                with pytest.raises(VerificationEvidenceRequiredError):
                    await AgentRunService(session).transition_status(
                        case.run_id, AgentRunStatus.VERIFYING, AgentRunStatus.SUCCEEDED,
                    )

            metadata = json.dumps([event.payload for event in events])
            logs = agent_log_capture.getvalue()
            assert "agent_usage_observed" in logs
            for private in (FILE, REPLACEMENT, "outside hunk", READ_ID, WRITE_ID, str(case.binding.root),
                            "synthetic-repository-test-key"):
                assert private not in metadata and private not in logs
        finally:
            await reading.dispose()

    database(check)


class _TimeoutProbe:
    """Use the real asyncio timeout and expire it only after the waiter is observable."""

    def __init__(self):
        self.armed = asyncio.Event()
        self.timer = None

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout_at(self, when):
        self.timer = asyncio.timeout_at(when)
        self.armed.set()
        return self.timer

    async def expire(self):
        await self.armed.wait()
        self.timer.reschedule(asyncio.get_running_loop().time())


def test_postgres_parallel_approved_runs_keep_their_own_outcomes(database, monkeypatch):
    async def check(case, _idle):
        second = replace(case, run_id=uuid4())
        published = {entry.run_id: asyncio.Event() for entry in (case, second)}
        publish = PendingApprovals.publish
        timers = {}

        def observe_publish(pending, operation):
            publish(pending, operation)
            published[operation.reference.ticket.run_id].set()

        class PerTaskTimeouts:
            def __getattr__(self, name):
                return getattr(asyncio, name)

            def timeout_at(self, deadline):
                timer = asyncio.timeout_at(deadline)
                timers[asyncio.current_task()] = timer
                return timer

        monkeypatch.setattr(PendingApprovals, "publish", observe_publish)
        monkeypatch.setattr(agent_service_module, "asyncio", PerTaskTimeouts())
        patch = _fixture_patch(case)
        async with case.factory.begin() as session:
            session.add(AgentRunModel(
                id=second.run_id, task_id=second.task_id, attempt_number=2, status=AgentRunStatus.QUEUED.value,
            ))
        reading = create_engine(case.settings)
        try:
            async with _offline_model(_patch_reply(patch, lambda: None)) as (left_model, left_calls):
                async with _offline_model(_patch_reply(patch, lambda: None)) as (right_model, right_calls):
                    left, right = _runner(case, left_model), _runner(second, right_model)
                    first_service, second_service = AgentService(case.factory, left), AgentService(case.factory, right)
                    first_task = asyncio.create_task(first_service.execute_run(case.run_id))
                    second_task = asyncio.create_task(second_service.execute_run(second.run_id))
                    try:
                        first_preview, second_preview = await asyncio.gather(
                            _wait_for_preview(left, first_task, published[case.run_id]),
                            _wait_for_preview(right, second_task, published[second.run_id]),
                        )
                        assert first_preview.reference.ticket.run_id == case.run_id
                        assert second_preview.reference.ticket.run_id == second.run_id
                        assert first_preview.reference.approval_id != second_preview.reference.approval_id
                        await left._decide(first_preview, ApprovalDecision.REJECT)
                        timers[second_task].reschedule(asyncio.get_running_loop().time())
                        await _graph_failure(first_task, "approval_rejected")
                        with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                            await second_task
                    finally:
                        for task in (first_task, second_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(first_task, second_task, return_exceptions=True)
                    assert left._approval_preview() is None and right._approval_preview() is None
            factory = create_session_factory(reading)
            first_state, second_state = await asyncio.gather(
                _approval_state(factory, case), _approval_state(factory, second),
            )
            _expect_write_outcome(
                first_state, run_status="failed", call_status="denied", approval_status="rejected",
                error="approval_rejected",
            )
            _expect_write_outcome(
                second_state, run_status="cancelled", call_status="failed", approval_status="rejected",
                error="cancelled",
            )
            assert [event.payload["code"] for event in first_state[3] if event.event_type == "agent_execution_failed"] == [
                "graph_execution_failed",
            ]
            assert first_state[4][0].id == first_preview.reference.approval_id
            assert second_state[4][0].id == second_preview.reference.approval_id
            assert len(left_calls) == len(right_calls) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL
        finally:
            await reading.dispose()
            async with case.factory.begin() as session:
                assert await session.scalar(text("SELECT current_database()")) == os.environ["POSTGRES_DB"]
                call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == second.run_id)
                await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
                await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == second.run_id))
                await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == second.run_id))
                await session.execute(delete(AgentRunModel).where(AgentRunModel.id == second.run_id))

    database(check)


@pytest.mark.parametrize("cause", ["approval-timeout", "user-cancel", "global-deadline"])
def test_postgres_approved_graph_wait_ends_without_write(database, monkeypatch, cause):
    async def check(case, idle):
        patch = _fixture_patch(case)
        apply = AsyncMock(side_effect=AssertionError("The write handler must not run"))
        monkeypatch.setattr(SafeRepositoryWriter, "apply", apply)
        timer = _TimeoutProbe()
        if cause == "approval-timeout":
            monkeypatch.setattr(pending_approvals_module, "asyncio", timer)
        elif cause == "global-deadline":
            monkeypatch.setattr(agent_service_module, "asyncio", timer)

        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (service, runner, task, published):
                await _wait_for_preview(runner, task, published)
                idle()
                if cause == "user-cancel":
                    await service.cancel_run(case.run_id)
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await timer.expire()
                    if cause == "global-deadline":
                        with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                            await task
                    else:
                        await _graph_failure(task, "approval_timeout")

        is_timeout = cause == "approval-timeout"
        _expect_write_outcome(
            await _approval_state(case.factory, case), run_status="failed" if is_timeout else "cancelled",
            call_status="failed", approval_status="rejected", error="approval_timeout" if is_timeout else "cancelled",
        )
        assert (case.binding.root / FILE).read_text() == ORIGINAL and len(calls) == 1
        apply.assert_not_called()

    database(check)


def test_postgres_approved_graph_deadline_preserves_already_committed_rejection(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        timer = _TimeoutProbe()
        monkeypatch.setattr(agent_service_module, "asyncio", timer)

        def miss_notification(_registry, _operation, decision):
            assert decision is ApprovalDecision.REJECT

        monkeypatch.setattr(PendingApprovals, "notify", miss_notification)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                await runner._decide(preview, ApprovalDecision.REJECT)
                assert not task.done()
                await timer.expire()
                with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$"):
                    await task
        state = await _approval_state(case.factory, case)
        _expect_write_outcome(
            state, run_status="failed", call_status="denied", approval_status="rejected", error="approval_rejected",
        )
        assert [event.payload["code"] for event in state[3] if event.event_type == "agent_execution_failed"] == [
            "execution_deadline_exceeded",
        ]
        assert (case.binding.root / FILE).read_text() == ORIGINAL and len(calls) == 1

    database(check)


def test_postgres_approved_graph_changes_to_source_invalidate_approval(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        target = case.binding.root / FILE
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                target.write_text("before\nchanged outside the hunk\n")
                await runner._decide(preview, ApprovalDecision.APPROVE)
                await _graph_failure(task, "approval_source_changed")
        audit = _expect_write_outcome(
            await _approval_state(case.factory, case),
            run_status="failed", call_status="failed", approval_status="approved", error="approval_source_changed",
        )
        assert [event.payload["status"] for event in audit] == ["proposed", "failed"]
        assert target.read_text() == "before\nchanged outside the hunk\n" and len(calls) == 1

    database(check)


def test_postgres_approved_graph_duplicate_decision_and_restart_have_no_authority(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        entered = asyncio.Event()

        async def hold_claim(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ApprovalGate, "claim", hold_claim)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (service, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                await runner._decide(preview, ApprovalDecision.APPROVE)
                await asyncio.wait_for(entered.wait(), 3)
                idle()
                with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                    await runner._decide(preview, ApprovalDecision.APPROVE)

                restarted = _runner(case, model)
                assert restarted._approval_preview() is None
                with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                    await restarted._decide(preview, ApprovalDecision.APPROVE)
                with pytest.raises(AgentRunnerError, match="^execution_failed$"):
                    await restarted.run(AgentRunRequest(case.task_id, case.run_id, "Restart attempt", ("Fixture",)))
                run, _, (call,), _, (approval,) = await _approval_state(case.factory, case)
                assert (run.status, call.status, approval.status) == ("waiting_approval", "proposed", "approved")
                await service.cancel_run(case.run_id)
                with pytest.raises(asyncio.CancelledError):
                    await task
        audit = _expect_write_outcome(
            await _approval_state(case.factory, case),
            run_status="cancelled", call_status="failed", approval_status="approved", error="cancelled",
        )
        assert [event.payload["status"] for event in audit] == ["proposed", "failed"]
        assert (case.binding.root / FILE).read_text() == ORIGINAL and len(calls) == 1

    database(check)


def test_postgres_approved_graph_concurrent_approval_observes_cancellation_lock(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        lock_held = asyncio.Event()
        permit_cancel = asyncio.Event()
        decision_started = asyncio.Event()
        pids = {}
        original_cancel = AgentRunService.cancel_execution
        original_load = ApprovalGate._load_locked

        async def hold_cancel(service, run_id):
            pids["cancel"] = await service.session.scalar(text("SELECT pg_backend_pid()"))
            assert await service.runs.get_for_update(run_id) is not None
            lock_held.set()
            await permit_cancel.wait()
            await original_cancel(service, run_id)

        async def track_load(gate, session, operation):
            if asyncio.current_task() is deciding:
                pids["decision"] = await session.scalar(text("SELECT pg_backend_pid()"))
                decision_started.set()
            return await original_load(gate, session, operation)

        async def confirm_blocking():
            async with case.factory() as observer, asyncio.timeout(3):
                while True:
                    blockers = await observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["decision"]},
                    )
                    if pids["cancel"] in blockers:
                        return
                    await asyncio.sleep(0.01)

        monkeypatch.setattr(AgentRunService, "cancel_execution", hold_cancel)
        monkeypatch.setattr(ApprovalGate, "_load_locked", track_load)
        deciding = cancelling = None
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (service, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                try:
                    cancelling = asyncio.create_task(service.cancel_run(case.run_id))
                    await asyncio.wait_for(lock_held.wait(), 3)
                    deciding = asyncio.create_task(runner._decide(preview, ApprovalDecision.APPROVE))
                    await asyncio.wait_for(decision_started.wait(), 3)
                    await confirm_blocking()
                    permit_cancel.set()
                    await cancelling
                    with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
                        await deciding
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    permit_cancel.set()
                    for operation in (deciding, cancelling):
                        if operation is not None:
                            if not operation.done():
                                operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
        _expect_write_outcome(
            await _approval_state(case.factory, case),
            run_status="cancelled", call_status="failed", approval_status="rejected", error="cancelled",
        )
        assert len(calls) == 1 and (case.binding.root / FILE).read_text() == ORIGINAL

    database(check)


def test_postgres_approved_graph_denies_multi_file_scope_before_creating_approval(database, monkeypatch):
    async def check(case, idle):
        other = "denied.txt"
        patch = _fixture_patch(case, FILE, other)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch, scopes=(PathScope(FILE),)) as (_, _, task, published):
                await _graph_failure(task, "permission_denied")
                assert not published.is_set()
        run, parent, tool_calls, events, approvals = await _approval_state(case.factory, case)
        assert (run.status, parent.status, run.final_result) == ("failed", "queued", None)
        assert not tool_calls and not approvals and len(calls) == 1
        assert not any(event.event_type.startswith(("tool_call_", "approval_")) for event in events)
        assert all((case.binding.root / path).read_text() == ORIGINAL for path in (FILE, other))

    database(check)


@pytest.mark.parametrize("phase", ["request", "claim", "final-audit"])
def test_postgres_approved_graph_persistence_failure_never_fabricates_success(database, monkeypatch, phase):
    async def check(case, idle):
        patch = _fixture_patch(case)
        original = AgentEventRepository.append
        injected = []
        timed_event = {"request": "approval_requested", "claim": "tool_call_started", "final-audit": "tool_call_finished"}

        async def invalid_event(repo, run_id, *, event_type, payload):
            event = await original(repo, run_id, event_type=event_type, payload=payload)
            if event_type == timed_event[phase] and not injected:
                injected.append(event_type)
                # Real PostgreSQL CHECK fails on the next flush or commit.
                event.sequence = 0
            return event

        monkeypatch.setattr(AgentEventRepository, "append", invalid_event)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                if phase != "request":
                    preview = await _wait_for_preview(runner, task, published)
                    await runner._decide(preview, ApprovalDecision.APPROVE)
                await _graph_failure(task, "audit_persistence_failed")
                assert published.is_set() == (phase != "request")
        state = await _approval_state(case.factory, case)
        run, parent, tool_calls, events, approvals = state
        assert (run.status, parent.status, run.final_result) == ("failed", "queued", None)
        assert injected == [timed_event[phase]] and len(calls) == 1
        expected = ORIGINAL
        if phase == "request":
            assert not tool_calls and not approvals
        elif phase == "claim":
            audit = _expect_write_outcome(
                state, run_status="failed", call_status="failed", approval_status="approved",
                error="audit_persistence_failed",
            )
            assert [event.payload["status"] for event in audit] == ["proposed", "failed"]
        else:
            (call,), (approval,) = tool_calls, approvals
            assert (call.status, approval.status) == ("running", "approved")
            assert [event.payload["status"] for event in _write_events(events, call)] == ["proposed", "running"]
            expected = f"{REPLACEMENT}\noutside hunk\n"
        assert (case.binding.root / FILE).read_text() == expected

    database(check)


@pytest.mark.parametrize("failure_point", ["handler-failed", "partial-apply"])
def test_postgres_approved_graph_write_failure_is_audited_without_retry(
    database, monkeypatch, agent_log_capture, failure_point,
):
    async def check(case, idle):
        other = "second.txt"
        patch = _fixture_patch(case, FILE, other)
        actual_replace = os.replace
        attempts = []
        secret = "synthetic-replace-error-private-message"
        partial = failure_point == "partial-apply"
        rejected_target = other if partial else FILE
        error_code = "patch_partial_apply" if partial else "handler_failed"

        def fail_replacement(source, target, **kwargs):
            if target in (FILE, other):
                attempts.append(target)
                if target == rejected_target:
                    raise OSError(secret)
            return actual_replace(source, target, **kwargs)

        monkeypatch.setattr(os, "replace", fail_replacement)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (_, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                await runner._decide(preview, ApprovalDecision.APPROVE)
                await _graph_failure(task, error_code)
        state = await _approval_state(case.factory, case)
        audit = _expect_write_outcome(
            state, run_status="failed", call_status="failed", approval_status="approved", error=error_code,
        )
        assert [event.payload["status"] for event in audit] == ["proposed", "running", "failed"]
        assert attempts == ([FILE, other] if partial else [FILE]) and len(calls) == 1
        assert (case.binding.root / FILE).read_text() == (f"{REPLACEMENT}\noutside hunk\n" if partial else ORIGINAL)
        assert (case.binding.root / other).read_text() == ORIGINAL
        metadata = json.dumps([event.payload for event in state[3]])
        logs = agent_log_capture.getvalue()
        for private in (secret, REPLACEMENT, FILE, other, str(case.binding.root)):
            assert private not in metadata and private not in logs

    database(check)


def test_postgres_approved_graph_cancel_after_mutation_keeps_observed_file_change(database, monkeypatch):
    async def check(case, idle):
        patch = _fixture_patch(case)
        changed = asyncio.Event()
        real_handler = ToolExecutor._invoke_handler

        async def pause_after_write(executor, tool, arguments, **kwargs):
            result = await real_handler(executor, tool, arguments, **kwargs)
            if tool.spec.name == "apply_patch":
                changed.set()
                await asyncio.Event().wait()
            return result

        monkeypatch.setattr(ToolExecutor, "_invoke_handler", pause_after_write)
        async with _offline_model(_patch_reply(patch, idle)) as (model, calls):
            async with _run_agent(case, model, monkeypatch) as (service, runner, task, published):
                preview = await _wait_for_preview(runner, task, published)
                await runner._decide(preview, ApprovalDecision.APPROVE)
                await asyncio.wait_for(changed.wait(), 3)
                await service.cancel_run(case.run_id)
                with pytest.raises(asyncio.CancelledError):
                    await task
        audit = _expect_write_outcome(
            await _approval_state(case.factory, case),
            run_status="cancelled", call_status="failed", approval_status="approved", error="cancelled",
        )
        assert [event.payload["status"] for event in audit] == ["proposed", "running", "failed"]
        assert (case.binding.root / FILE).read_text() == f"{REPLACEMENT}\noutside hunk\n" and len(calls) == 1

    database(check)
