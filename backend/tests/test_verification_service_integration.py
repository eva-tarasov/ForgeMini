"""Durable verification on the existing guarded Week 4 database."""

import asyncio
import json
import os
import re
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.settings import Settings
from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.domain.transitions import InvalidAgentRunTransition
from app.permissions.context import PathScope, PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.agent_result import AgentFinalResult
from app.services.agent_runs import (
    AgentRunNotFoundError, AgentRunService, StaleAgentRunStatusError, VerificationEvidenceRequiredError,
)
from app.services.approval_contracts import ApprovalDecision
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_calls import ToolCallService
from app.services.verification_contract import (
    FileDigestCheck, VerificationBinding, VerificationContractError, VerificationPlan,
    VerificationVerdict, fingerprint_criteria,
)
from app.services.verification_evidence import VERIFICATION_RECORDED, decode_evidence
from app.services.verification_observer import TrustedFixtureAccess
from app.services.verification_service import VerificationService
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor, create_fixture_patch_tool
from test_approval_contracts import patch_for


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to the dedicated Week 4 DB",
)
READY = AgentFinalResult(
    summary="synthetic preliminary", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)
CONTENT = b"before\noutside hunk\n"
CRITERIA = ("Exact trusted fixture bytes",)


@dataclass
class Case:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    root: Path
    binding: VerificationBinding
    plan: VerificationPlan
    access: TrustedFixtureAccess
    attempt: UUID

    async def verify(self, *, attempt=None, isolated=True, plan=None, access=None, factory=None):
        return await VerificationService(factory or self.factory).verify(
            plan or self.plan, access or self.access, attempt_id=attempt or self.attempt,
            isolated_fixture=isolated,
        )


@pytest.fixture
def database(tmp_path):
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_week4_audit_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("Expected guarded dedicated Week 4 PostgreSQL database", pytrace=False)
    settings = Settings(postgres_db=name)

    def run(scenario, *, write=False):
        async def check():
            engine = create_engine(settings)
            factory = create_session_factory(engine)
            run_id, task_id, workspace_id = uuid4(), uuid4(), uuid4()
            binding = VerificationBinding(run_id, task_id, workspace_id, "fixture-v1")
            plan = VerificationPlan(
                uuid4(), binding, fingerprint_criteria(CRITERIA),
                (FileDigestCheck(uuid4(), 0, "out.txt", sha256(CONTENT).hexdigest()),),
            )
            access = TrustedFixtureAccess(
                plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=binding,
                root=tmp_path, read_scopes=(PathScope("out.txt"),), acceptance_criteria=CRITERIA,
            )
            case = Case(settings, factory, tmp_path, binding, plan, access, uuid4())
            (tmp_path / "out.txt").write_bytes(CONTENT)
            try:
                async with asyncio.timeout(30):
                    async with factory.begin() as session:
                        assert await session.scalar(text("SELECT current_database()")) == name
                        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "1d972cc34fbe"
                        session.add(WorkspaceModel(id=workspace_id, name="verification fixture"))
                        await session.flush()
                        session.add(TaskModel(
                            id=task_id, workspace_id=workspace_id, goal="Verify disposable fixture",
                            acceptance_criteria=list(CRITERIA),
                        ))
                        await session.flush()
                        session.add(AgentRunModel(
                            id=run_id, task_id=task_id, attempt_number=1, status="queued",
                        ))
                    async with factory() as session:
                        await AgentRunService(session).claim_for_execution(run_id)
                    context = PermissionContext(
                        run_id, workspace_id, frozenset(Capability),
                        read_scopes=(PathScope(".", recursive=True),),
                        write_scopes=(PathScope(".", recursive=True),),
                    )
                    if write:
                        coordinator = ApprovedToolExecutor(
                            ToolExecutor(ToolRegistry([create_fixture_patch_tool(root=tmp_path, context=context)])),
                            factory,
                        )
                        operation = asyncio.create_task(coordinator.execute(
                            "apply_patch", {"patch": patch_for("out.txt", after="approved")}, context=context,
                        ))
                        try:
                            while coordinator.pending_request(context) is None:
                                if operation.done():
                                    await operation
                                await asyncio.sleep(0)
                            reference = coordinator.pending_request(context)
                            await coordinator.decide(
                                reference, ApprovalDecision.APPROVE, context=context,
                            )
                            await operation
                        finally:
                            if not operation.done():
                                operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                        digest = sha256((tmp_path / "out.txt").read_bytes()).hexdigest()
                        case.plan = replace(plan, checks=(replace(plan.checks[0], expected_sha256=digest),))
                        case.access = replace(access, plan_fingerprint=case.plan.fingerprint)
                    else:
                        executor = create_fixture_executor(
                            root=tmp_path, context=context, baseline=FixtureBaseline("fixture-v1", {}),
                        )
                        await AuditedToolExecutor(executor, ToolCallService(factory)).execute(
                            "read_file", {"path": "out.txt"}, context=context,
                        )
                    async with factory() as session:
                        await AgentRunService(session).complete_execution(run_id, READY)
                    await scenario(case)
            finally:
                try:
                    async with asyncio.timeout(10), factory.begin() as session:
                        ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == run_id)
                        await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(ids)))
                        await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == run_id))
                        await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == run_id))
                        await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
                        await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
                        await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
                finally:
                    await engine.dispose()
        asyncio.run(check())
    return run


async def snapshot(case, factory=None):
    async with (factory or case.factory)() as session:
        run = await session.get(AgentRunModel, case.binding.run_id)
        task = await session.get(TaskModel, case.binding.task_id)
        events = (await session.execute(select(
            AgentEventModel.sequence, AgentEventModel.event_type, AgentEventModel.payload,
        ).where(AgentEventModel.agent_run_id == run.id).order_by(AgentEventModel.sequence))).all()
        return run.status, task.status, events


def evidence_from(view):
    return [decode_evidence(row.payload) for row in view[2] if row.event_type == VERIFICATION_RECORDED]


@pytest.mark.parametrize("verdict", ["pass", "fail", "inconclusive"])
def test_postgres_verification_verdict_and_fresh_engine_visibility(database, verdict):
    async def scenario(case):
        if verdict == "fail":
            (case.root / "out.txt").write_bytes(b"other")
        elif verdict == "inconclusive":
            (case.root / "out.txt").unlink()
        record = await case.verify()
        assert record.verdict.value == verdict
        engine = create_engine(case.settings)
        try:
            view = await snapshot(case, create_session_factory(engine))
        finally:
            await engine.dispose()
        expected = {"pass": "succeeded", "fail": "failed", "inconclusive": "verifying"}[verdict]
        assert view[:2] == (expected, "queued")
        assert evidence_from(view) == [record]
        assert [row.sequence for row in view[2]] == list(range(1, len(view[2]) + 1))
        assert CONTENT.decode() not in json.dumps([row.payload for row in view[2]])
        assert str(case.root) not in json.dumps([row.payload for row in view[2]])
    database(scenario)


def test_postgres_verification_approved_write_has_complete_audit(database):
    async def scenario(case):
        evidence = await case.verify()
        assert evidence.audit_complete and evidence.verdict is VerificationVerdict.PASS
        assert (await snapshot(case))[0] == "succeeded"
    database(scenario, write=True)


@pytest.mark.parametrize("damage", [
    "missing-result", "invalid-result", "blocked-result", "missing-final-audit", "running",
    "failed", "denied", "foreign-call-event", "duplicate-finish", "missing-result-event",
])
def test_postgres_verification_rejects_or_keeps_inconclusive_for_bad_evidence(database, damage):
    async def scenario(case):
        async with case.factory.begin() as session:
            run = await session.get(AgentRunModel, case.binding.run_id)
            call = await session.scalar(select(ToolCallModel).where(ToolCallModel.agent_run_id == run.id))
            finished = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == run.id, AgentEventModel.event_type == "tool_call_finished",
            ))
            if damage == "missing-result":
                run.final_result = None
            elif damage == "invalid-result":
                run.final_result = {"schema_version": 44}
            elif damage == "blocked-result":
                from app.services.agent_result_codec import encode_agent_result
                run.final_result = encode_agent_result(READY.model_copy(update={"status": "blocked", "blocked_reason": "blocked"}))
            elif damage == "missing-final-audit":
                await session.delete(finished)
            elif damage == "missing-result-event":
                await session.execute(delete(AgentEventModel).where(
                    AgentEventModel.agent_run_id == run.id, AgentEventModel.event_type == "agent_result_recorded",
                ))
            elif damage == "foreign-call-event":
                finished.payload = {**finished.payload, "tool_call_id": str(uuid4())}
            elif damage == "duplicate-finish":
                await AgentEventRepository(session).append(
                    run.id, event_type=finished.event_type, payload=finished.payload,
                )
            else:
                call.status = damage
        if damage.endswith("result"):
            with pytest.raises(VerificationContractError):
                await case.verify()
            assert evidence_from(await snapshot(case)) == []
        else:
            result = await case.verify()
            assert result.file_verdict is VerificationVerdict.PASS
            assert result.verdict is VerificationVerdict.INCONCLUSIVE and not result.audit_complete
        assert (await snapshot(case))[:2] == ("verifying", "queued")
    database(scenario)


@pytest.mark.parametrize("damage", ["pending", "rejected", "missing-decision", "foreign-workspace", "wrong-hash"])
def test_postgres_verification_broken_approval_never_succeeds(database, damage):
    async def scenario(case):
        async with case.factory.begin() as session:
            approval = await session.scalar(select(ApprovalModel).join(ToolCallModel).where(
                ToolCallModel.agent_run_id == case.binding.run_id,
            ))
            if damage in {"pending", "rejected"}:
                approval.status = damage
            else:
                kind = "approval_decided" if damage == "missing-decision" else "approval_requested"
                event = await session.scalar(select(AgentEventModel).where(
                    AgentEventModel.agent_run_id == case.binding.run_id, AgentEventModel.event_type == kind,
                ))
                if damage == "missing-decision":
                    await session.delete(event)
                else:
                    key = "workspace_id" if damage == "foreign-workspace" else "patch_sha256"
                    value = str(uuid4()) if key == "workspace_id" else "invalid"
                    event.payload = {**event.payload, key: value}
        record = await case.verify()
        assert record.verdict is VerificationVerdict.INCONCLUSIVE
        assert (await snapshot(case))[0] == "verifying"
    database(scenario, write=True)


@pytest.mark.parametrize("foreign", ["run", "task", "workspace", "criteria", "revision"])
def test_postgres_verification_sql_binding_prevents_observation(database, monkeypatch, foreign):
    from app.services import verification_service as module

    async def scenario(case):
        async def unexpected(*args):
            pytest.fail("Invalid SQL binding must fail before observation")
        monkeypatch.setattr(module, "observe", unexpected)
        if foreign == "criteria":
            async with case.factory.begin() as session:
                task = await session.get(TaskModel, case.binding.task_id)
                task.acceptance_criteria = ["different"]
            plan, access = case.plan, case.access
        elif foreign == "revision":
            plan, access = case.plan, replace(case.access, binding=replace(case.binding, baseline_revision="other"))
        else:
            binding = replace(case.binding, **{f"{foreign}_id": uuid4()})
            plan = replace(case.plan, binding=binding)
            access = replace(case.access, binding=binding, plan_fingerprint=plan.fingerprint)
        with pytest.raises((VerificationContractError, AgentRunNotFoundError)):
            await case.verify(plan=plan, access=access)
        assert evidence_from(await snapshot(case)) == []
    database(scenario)


def test_postgres_verification_requires_explicit_fixture_isolation(database):
    async def scenario(case):
        result = await case.verify(isolated=False)
        assert result.file_verdict is VerificationVerdict.PASS
        assert result.verdict is VerificationVerdict.INCONCLUSIVE
        assert (await snapshot(case))[0] == "verifying"
    database(scenario)


def test_postgres_verification_idempotent_replay_and_conflicting_identity(database, monkeypatch):
    from app.services import verification_service as module

    async def scenario(case):
        result = await case.verify()
        before = await snapshot(case)
        async def unexpected(*args):
            pytest.fail("Idempotent replay must not observe again")
        monkeypatch.setattr(module, "observe", unexpected)
        assert await case.verify() == result
        changed = replace(case.plan, checks=(replace(case.plan.checks[0], expected_sha256="0" * 64),))
        with pytest.raises(VerificationContractError, match="verification_attempt_conflict"):
            await case.verify(plan=changed, access=replace(case.access, plan_fingerprint=changed.fingerprint))
        assert await snapshot(case) == before
    database(scenario)


def test_postgres_verification_new_attempt_after_inconclusive_and_duplicate_event(database):
    async def scenario(case):
        first = await case.verify(isolated=False)
        assert first.verdict is VerificationVerdict.INCONCLUSIVE
        second = await case.verify(attempt=uuid4())
        assert second.verdict is VerificationVerdict.PASS
        async with case.factory.begin() as session:
            original = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == case.binding.run_id,
                AgentEventModel.event_type == VERIFICATION_RECORDED,
            ).order_by(AgentEventModel.sequence))
            await AgentEventRepository(session).append(
                case.binding.run_id, event_type=VERIFICATION_RECORDED, payload=original.payload,
            )
        with pytest.raises(VerificationContractError, match="duplicate_verification_evidence"):
            await case.verify(isolated=False)
    database(scenario)


@pytest.mark.parametrize("same_attempt", [False, True], ids=["different-attempts", "same-attempt"])
def test_postgres_concurrent_verifiers_serialize_without_duplicate_evidence(database, monkeypatch, same_attempt):
    from app.services import verification_service as module

    async def scenario(case):
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        original = module.observe

        async def barrier(*args):
            nonlocal count
            result = await original(*args)
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(module, "observe", barrier)
        tasks = [
            asyncio.create_task(case.verify()),
            asyncio.create_task(case.verify(attempt=case.attempt if same_attempt else uuid4())),
        ]
        try:
            await entered.wait()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == (2 if same_attempt else 1)
        if not same_attempt:
            assert any(isinstance(result, StaleAgentRunStatusError) for result in results)
        view = await snapshot(case)
        assert view[0] == "succeeded" and len(evidence_from(view)) == 1
    database(scenario)


@pytest.mark.parametrize("winner", ["cancel", "verify"])
def test_postgres_verify_cancel_commit_order_preserves_terminal_state(database, monkeypatch, winner):
    from app.services import verification_service as module

    async def scenario(case):
        observed, release = asyncio.Event(), asyncio.Event()
        original = module.observe

        async def barrier(*args):
            result = await original(*args)
            observed.set()
            await release.wait()
            return result

        monkeypatch.setattr(module, "observe", barrier)
        task = asyncio.create_task(case.verify())
        try:
            await observed.wait()
            if winner == "cancel":
                async with case.factory() as session:
                    await AgentRunService(session).cancel_execution(case.binding.run_id)
                release.set()
                with pytest.raises(StaleAgentRunStatusError):
                    await task
                assert (await snapshot(case))[0] == "cancelled"
                assert evidence_from(await snapshot(case)) == []
            else:
                release.set()
                await task
                async with case.factory() as session:
                    with pytest.raises(InvalidAgentRunTransition):
                        await AgentRunService(session).cancel_execution(case.binding.run_id)
                assert (await snapshot(case))[0] == "succeeded"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    database(scenario)


@pytest.mark.parametrize("failure", ["evidence", "status-event", "transition", "commit"])
def test_postgres_verification_rolls_back_every_partial_phase(database, monkeypatch, failure):
    async def scenario(case):
        before = await snapshot(case)
        append = AgentEventRepository.append

        async def broken_append(repository, run_id, *, event_type, payload):
            record = await append(repository, run_id, event_type=event_type, payload=payload)
            if event_type == (VERIFICATION_RECORDED if failure == "evidence" else "agent_run_status_changed"):
                await repository.session.flush()
                assert await snapshot(case) == before
                raise RuntimeError("injected persistence failure")
            return record

        class BrokenCommit(AsyncSession):
            async def commit(self):
                await self.flush()
                assert await snapshot(case) == before
                raise RuntimeError("injected commit failure")

        if failure in {"evidence", "status-event"}:
            monkeypatch.setattr(AgentEventRepository, "append", broken_append)
        elif failure == "transition":
            from app.services import verification_service as module
            def broken_transition(*args):
                raise RuntimeError("injected transition failure")
            monkeypatch.setattr(module, "validate_agent_run_transition", broken_transition)
        factory = async_sessionmaker(
            case.factory.kw["bind"], class_=BrokenCommit, expire_on_commit=False,
        ) if failure == "commit" else case.factory
        with pytest.raises(RuntimeError, match="injected"):
            await case.verify(factory=factory)
        assert await snapshot(case) == before
    database(scenario)


def test_postgres_observation_has_no_open_sql_session_and_cancellation_persists_nothing(database, monkeypatch):
    from app.services import verification_service as module

    async def scenario(case):
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
        factory = async_sessionmaker(case.factory.kw["bind"], class_=TrackedSession, expire_on_commit=False)
        cancelled = asyncio.CancelledError()
        async def cancel(*args):
            assert not open_sessions
            raise cancelled
        monkeypatch.setattr(module, "observe", cancel)
        before = await snapshot(case)
        with pytest.raises(asyncio.CancelledError) as caught:
            await case.verify(factory=factory)
        assert caught.value is cancelled
        assert not open_sessions and await snapshot(case) == before
    database(scenario)


def test_postgres_direct_success_guard_remains_and_task_is_unchanged(database):
    async def scenario(case):
        async with case.factory() as session:
            with pytest.raises(VerificationEvidenceRequiredError):
                await AgentRunService(session).transition_status(
                    case.binding.run_id, AgentRunStatus.VERIFYING, AgentRunStatus.SUCCEEDED,
                )
        assert (await snapshot(case))[:2] == ("verifying", "queued")
        assert (await case.verify()).verdict is VerificationVerdict.PASS
    database(scenario)


@pytest.mark.parametrize("change", ["audit", "criteria", "result", "pending-proposal", "partial-effect"])
def test_postgres_verification_rechecks_data_after_observation(database, monkeypatch, change):
    from app.services import verification_service as module

    async def scenario(case):
        original = module.observe

        async def changed(*args):
            observations = await original(*args)
            async with case.factory.begin() as session:
                if change == "criteria":
                    task = await session.get(TaskModel, case.binding.task_id)
                    task.acceptance_criteria = ["changed"]
                elif change == "result":
                    run = await session.get(AgentRunModel, case.binding.run_id)
                    payload = dict(run.final_result)
                    payload["result"] = {**payload["result"], "summary": "new preliminary"}
                    run.final_result = payload
                elif change == "audit":
                    event = await session.scalar(select(AgentEventModel).where(
                        AgentEventModel.agent_run_id == case.binding.run_id,
                        AgentEventModel.event_type == "tool_call_finished",
                    ))
                    event.payload = {**event.payload, "duration_ms": event.payload.get("duration_ms", 0) + 1}
                elif change == "pending-proposal":
                    session.add(ToolCallModel(
                        id=uuid4(), agent_run_id=case.binding.run_id, tool_name="apply_patch", status="proposed",
                    ))
                else:
                    call = await session.scalar(select(ToolCallModel).where(
                        ToolCallModel.agent_run_id == case.binding.run_id,
                    ))
                    call.status = "failed"
                    event = await session.scalar(select(AgentEventModel).where(
                        AgentEventModel.agent_run_id == case.binding.run_id,
                        AgentEventModel.event_type == "tool_call_finished",
                    ))
                    event.payload = {**event.payload, "status": "failed", "error_code": "patch_partial_apply"}
            return observations

        monkeypatch.setattr(module, "observe", changed)
        if change in {"criteria", "result"}:
            with pytest.raises(VerificationContractError):
                await case.verify()
            assert evidence_from(await snapshot(case)) == []
        else:
            result = await case.verify()
            assert result.file_verdict is VerificationVerdict.PASS
            assert result.verdict is VerificationVerdict.INCONCLUSIVE
        assert (await snapshot(case))[0] == "verifying"
    database(scenario)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_postgres_verification_requires_exact_audit_schema_types(database, version):
    async def scenario(case):
        async with case.factory.begin() as session:
            event = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == case.binding.run_id,
                AgentEventModel.event_type == "tool_call_finished",
            ))
            event.payload = {**event.payload, "schema_version": version}
            flag_modified(event, "payload")
        assert (await case.verify()).verdict is VerificationVerdict.INCONCLUSIVE
    database(scenario)


@pytest.mark.parametrize("winner", ["cancel", "verify"])
def test_postgres_verification_waits_on_actual_parent_row_lock(database, monkeypatch, winner):
    async def scenario(case):
        acquired, release = asyncio.Event(), asyncio.Event()
        original = AgentRunRepository.get_for_update
        pids = {}

        async def hold(repository, key):
            name = asyncio.current_task().get_name()
            pids[name] = await repository.session.scalar(text("SELECT pg_backend_pid()"))
            row = await original(repository, key)
            if name == winner:
                acquired.set()
                await release.wait()
            return row

        async def cancel():
            async with case.factory() as session:
                await AgentRunService(session).cancel_execution(case.binding.run_id)

        monkeypatch.setattr(AgentRunRepository, "get_for_update", hold)
        actions = {"verify": case.verify, "cancel": cancel}
        loser = "verify" if winner == "cancel" else "cancel"
        tasks = []
        try:
            tasks.append(asyncio.create_task(actions[winner](), name=winner))
            await acquired.wait()
            tasks.append(asyncio.create_task(actions[loser](), name=loser))
            async with case.factory() as observer:
                while True:
                    if loser in pids:
                        blocked = await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[loser]},
                        )
                        if pids[winner] in blocked:
                            break
                    assert not tasks[1].done()
                    await asyncio.sleep(0.01)
            release.set()
            await tasks[0]
            with pytest.raises(StaleAgentRunStatusError if loser == "verify" else InvalidAgentRunTransition):
                await tasks[1]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert (await snapshot(case))[0] == ("cancelled" if winner == "cancel" else "succeeded")
        assert len(evidence_from(await snapshot(case))) == (0 if winner == "cancel" else 1)
    database(scenario)


def test_postgres_verification_real_event_constraint_rolls_back_evidence_and_status(database, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    async def scenario(case):
        before = await snapshot(case)
        append = AgentEventRepository.append

        async def duplicate_sequence(repository, run_id, *, event_type, payload):
            event = await append(repository, run_id, event_type=event_type, payload=payload)
            if event_type == VERIFICATION_RECORDED:
                event.sequence = 1
            return event

        monkeypatch.setattr(AgentEventRepository, "append", duplicate_sequence)
        with pytest.raises(IntegrityError) as caught:
            await case.verify()
        assert caught.value.orig.sqlstate == "23505"
        assert await snapshot(case) == before
    database(scenario)


def test_postgres_verification_lost_commit_acknowledgment_replays_durable_attempt(database):
    async def scenario(case):
        class LostAcknowledgment(AsyncSession):
            async def commit(self):
                await super().commit()
                raise RuntimeError("lost commit acknowledgment")
        factory = async_sessionmaker(
            case.factory.kw["bind"], class_=LostAcknowledgment, expire_on_commit=False,
        )
        with pytest.raises(RuntimeError, match="lost commit"):
            await case.verify(factory=factory)
        view = await snapshot(case)
        assert view[0] == "succeeded"
        [stored] = evidence_from(view)
        assert await case.verify() == stored
        assert await snapshot(case) == view
    database(scenario)


def test_postgres_verification_checks_prior_evidence_even_for_new_attempt(database):
    async def scenario(case):
        await case.verify(isolated=False)
        async with case.factory.begin() as session:
            event = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == case.binding.run_id,
                AgentEventModel.event_type == VERIFICATION_RECORDED,
            ))
            event.payload = {**event.payload, "verdict": "pass"}
        assert (await case.verify(attempt=uuid4())).verdict is VerificationVerdict.INCONCLUSIVE
    database(scenario)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_postgres_verification_never_reopens_terminal_run(database, terminal):
    async def scenario(case):
        async with case.factory.begin() as session:
            run = await session.get(AgentRunModel, case.binding.run_id)
            run.status = terminal
        before = await snapshot(case)
        with pytest.raises(StaleAgentRunStatusError):
            await case.verify()
        assert await snapshot(case) == before
    database(scenario)


def test_postgres_cancellation_during_publication_rolls_back_all_staged_writes(database, monkeypatch):
    async def scenario(case):
        before = await snapshot(case)
        staged, release = asyncio.Event(), asyncio.Event()
        append = AgentEventRepository.append

        async def paused(repository, run_id, *, event_type, payload):
            event = await append(repository, run_id, event_type=event_type, payload=payload)
            if event_type == VERIFICATION_RECORDED:
                await repository.session.flush()
                staged.set()
                await release.wait()
            return event

        monkeypatch.setattr(AgentEventRepository, "append", paused)
        task = asyncio.create_task(case.verify())
        try:
            await staged.wait()
            assert await snapshot(case) == before
            task.cancel("cancel publication")
            with pytest.raises(asyncio.CancelledError, match="cancel publication"):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert await snapshot(case) == before
    database(scenario)


def test_postgres_verification_requires_running_admission_before_tool_proposal(database):
    async def scenario(case):
        async with case.factory.begin() as session:
            running = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == case.binding.run_id, AgentEventModel.sequence == 1,
            ))
            proposed = await session.scalar(select(AgentEventModel).where(
                AgentEventModel.agent_run_id == case.binding.run_id, AgentEventModel.event_type == "tool_call_proposed",
            ))
            proposed_sequence = proposed.sequence
            running.sequence = 100
            await session.flush()
            proposed.sequence = 1
            await session.flush()
            running.sequence = proposed_sequence
        result = await case.verify()
        assert result.file_verdict is VerificationVerdict.PASS
        assert result.verdict is VerificationVerdict.INCONCLUSIVE
        assert (await snapshot(case))[0] == "verifying"
    database(scenario)
