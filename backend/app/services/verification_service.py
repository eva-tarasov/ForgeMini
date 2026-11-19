"""Short PostgreSQL phases around independent observation of an isolated fixture."""

from os import fsencode
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel
from app.domain.execution import AgentRunStatus, ApprovalStatus, ToolCallStatus
from app.domain.transitions import validate_agent_run_transition
from app.permissions.context import PathScope
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.agent_result_codec import AgentResultContractError, decode_agent_result
from app.services.agent_runs import AgentRunNotFoundError, StaleAgentRunStatusError
from app.services.approval_contracts import ApprovalReference
from app.services.tool_call_audit import ToolCallAudit, encode_tool_call_event
from app.services.tool_calls import ToolCallTicket
from app.services.verification_contract import (
    VerificationBinding, VerificationContractError, VerificationPlan, VerificationVerdict, fingerprint_criteria,
)
from app.services.verification_evidence import (
    VERIFICATION_RECORDED, VerificationEvidence, decode_evidence, encode_evidence, metadata_digest,
)
from app.services.verification_observer import TrustedFixtureAccess, observe


MAX_AUDIT_EVENTS = 512
MAX_AUDIT_CALLS = 32
_READ_TOOLS = frozenset({"read_file", "list_files", "search_files", "git_diff"})
_TOOL_EVENTS = frozenset({"tool_call_proposed", "tool_call_started", "tool_call_finished"})
_APPROVAL_EVENTS = frozenset({"approval_requested", "approval_decided"})
_EVENT_RUN_STATUS = {
    "tool_call_proposed": AgentRunStatus.RUNNING,
    "tool_call_started": AgentRunStatus.RUNNING,
    "tool_call_finished": AgentRunStatus.RUNNING,
    "approval_requested": AgentRunStatus.RUNNING,
    "approval_decided": AgentRunStatus.WAITING_APPROVAL,
    "agent_result_recorded": AgentRunStatus.RUNNING,
    VERIFICATION_RECORDED: AgentRunStatus.VERIFYING,
}


class VerificationService:
    """Own load, observation and locked publication. Caller owns fixture isolation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def verify(
        self, plan: VerificationPlan, access: TrustedFixtureAccess, *,
        attempt_id: UUID, isolated_fixture: bool = False,
    ) -> VerificationEvidence:
        if type(attempt_id) is not UUID or attempt_id.int == 0 or type(isolated_fixture) is not bool:
            raise VerificationContractError("invalid_verification_attempt")
        # Validate trusted access without opening its root.
        _check_access(plan, access)
        access_sha256 = metadata_digest({
            "root": str(access.root),
            "read_scopes": [[scope.path, scope.recursive] for scope in access.read_scopes],
        })
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            run = await AgentRunRepository(session).get(plan.binding.run_id)
            await _validate_run(session, run, plan)
            replay = await _replay(session, run, plan, attempt_id, access_sha256, isolated_fixture)
            if replay is not None:
                return replay
            _require_verifying(run)
            result_sha256 = metadata_digest(run.final_result)
            _, audit_sha256 = await _audit(session, run, plan)
        observations = await observe(plan, access)

        async with self._session_factory() as session:
            try:
                run = await AgentRunRepository(session).get_for_update(plan.binding.run_id)
                await _validate_run(session, run, plan, lock_task=True)
                replay = await _replay(session, run, plan, attempt_id, access_sha256, isolated_fixture)
                if replay is not None:
                    return replay
                _require_verifying(run)
                if metadata_digest(run.final_result) != result_sha256:
                    raise VerificationContractError("verification_result_changed")
                audit_complete, current_audit_sha256 = await _audit(session, run, plan)
                evidence = VerificationEvidence(
                    attempt_id, plan, observations,
                    audit_complete and current_audit_sha256 == audit_sha256,
                    isolated_fixture, current_audit_sha256, access_sha256, result_sha256,
                )
                events = AgentEventRepository(session)
                await events.append(run.id, event_type=VERIFICATION_RECORDED, payload=encode_evidence(evidence))
                if evidence.verdict is not VerificationVerdict.INCONCLUSIVE:
                    target = AgentRunStatus.SUCCEEDED if evidence.verdict is VerificationVerdict.PASS else AgentRunStatus.FAILED
                    validate_agent_run_transition(AgentRunStatus(run.status), target)
                    run.status = target.value
                    await events.add_status_changed(run.id, AgentRunStatus.VERIFYING, target)
                await session.commit()
                return evidence
            except BaseException as error:
                try:
                    await session.rollback()
                except Exception:
                    error.add_note("verification_rollback_failed")
                raise


def _check_access(plan: VerificationPlan, access: TrustedFixtureAccess) -> None:
    if (
        type(plan) is not VerificationPlan or type(access) is not TrustedFixtureAccess
        or type(access.binding) is not VerificationBinding
        or type(access.plan_id) is not UUID or access.plan_id.int == 0
        or type(access.plan_fingerprint) is not str
        or not isinstance(access.root, Path) or not access.root.is_absolute()
        or type(access.read_scopes) is not tuple
        or any(type(scope) is not PathScope for scope in access.read_scopes)
    ):
        raise VerificationContractError("invalid_verification_access")
    try:
        if b"\0" in fsencode(access.root):
            raise ValueError
    except (UnicodeEncodeError, ValueError):
        raise VerificationContractError("invalid_verification_access") from None
    if (
        access.plan_id != plan.plan_id or access.plan_fingerprint != plan.fingerprint or access.binding != plan.binding
        or fingerprint_criteria(access.acceptance_criteria) != plan.criterion_digests
    ):
        raise VerificationContractError("verification_access_mismatch")


async def _validate_run(
    session: AsyncSession, run: AgentRunModel | None, plan: VerificationPlan, *, lock_task: bool = False,
) -> None:
    if run is None:
        raise AgentRunNotFoundError("agent_run_not_found")
    statement = select(TaskModel).where(TaskModel.id == run.task_id)
    if lock_task:
        statement = statement.with_for_update(read=True)
    task = await session.scalar(statement)
    if (
        task is None or (run.id, run.task_id, task.workspace_id) != (
            plan.binding.run_id, plan.binding.task_id, plan.binding.workspace_id,
        ) or fingerprint_criteria(tuple(task.acceptance_criteria)) != plan.criterion_digests
    ):
        raise VerificationContractError("verification_sql_binding_mismatch")
    try:
        result = decode_agent_result(run.final_result)
    except AgentResultContractError:
        raise VerificationContractError("verification_preliminary_result_required") from None
    if result.status != "ready_for_verification":
        raise VerificationContractError("verification_preliminary_result_required")


def _require_verifying(run: AgentRunModel) -> None:
    if run.status != AgentRunStatus.VERIFYING.value:
        raise StaleAgentRunStatusError(AgentRunStatus.VERIFYING, AgentRunStatus(run.status))


async def _replay(
    session: AsyncSession, run: AgentRunModel, plan: VerificationPlan,
    attempt_id: UUID, access_sha256: str, isolated: bool,
) -> VerificationEvidence | None:
    events = list((await session.scalars(select(AgentEventModel).where(
        AgentEventModel.agent_run_id == plan.binding.run_id,
        AgentEventModel.event_type == VERIFICATION_RECORDED,
        AgentEventModel.payload["attempt_id"].astext == str(attempt_id),
    ).limit(2))).all())
    if len(events) > 1:
        raise VerificationContractError("duplicate_verification_evidence")
    if not events:
        return None
    stored = decode_evidence(events[0].payload)
    if (
        stored.plan != plan or stored.access_sha256 != access_sha256
        or stored.isolated_fixture is not isolated or stored.attempt_id != attempt_id
    ):
        raise VerificationContractError("verification_attempt_conflict")
    if stored.result_sha256 != metadata_digest(run.final_result):
        raise VerificationContractError("verification_result_changed")
    if stored.verdict is not VerificationVerdict.INCONCLUSIVE:
        target = AgentRunStatus.SUCCEEDED if stored.verdict is VerificationVerdict.PASS else AgentRunStatus.FAILED
        following = await session.scalar(select(AgentEventModel).where(
            AgentEventModel.agent_run_id == run.id,
            AgentEventModel.sequence == events[0].sequence + 1,
        ))
        if (
            run.status != target.value or following is None
            or following.event_type != "agent_run_status_changed"
            or following.payload != {"from": "verifying", "to": target.value}
        ):
            raise VerificationContractError("invalid_verification_finalization")
    return stored


async def _audit(session: AsyncSession, run: AgentRunModel, plan: VerificationPlan) -> tuple[bool, str]:
    calls = list((await session.scalars(select(ToolCallModel).where(
        ToolCallModel.agent_run_id == run.id,
    ).order_by(ToolCallModel.id).limit(MAX_AUDIT_CALLS + 1))).all())
    approvals = list((await session.scalars(select(ApprovalModel).join(ToolCallModel).where(
        ToolCallModel.agent_run_id == run.id,
    ).order_by(ApprovalModel.id).limit(MAX_AUDIT_CALLS + 1))).all())
    events = list((await session.scalars(select(AgentEventModel).where(
        AgentEventModel.agent_run_id == run.id,
    ).order_by(AgentEventModel.sequence).limit(MAX_AUDIT_EVENTS + 1))).all())
    digest = metadata_digest({
        "calls": [[str(call.id), call.tool_name, call.status] for call in calls],
        "approvals": [[str(item.id), str(item.tool_call_id), item.status,
                       item.decided_at.isoformat() if item.decided_at else None] for item in approvals],
        "events": [[item.sequence, item.event_type, item.payload] for item in events
                   if item.event_type != VERIFICATION_RECORDED],
    })
    if len(calls) > MAX_AUDIT_CALLS or len(approvals) > MAX_AUDIT_CALLS or len(events) > MAX_AUDIT_EVENTS:
        return False, digest
    try:
        complete = _complete_audit(calls, approvals, events, plan)
    except (ValueError, TypeError, KeyError, AttributeError):
        complete = False
    return complete, digest


def _complete_audit(
    calls: list[ToolCallModel], approvals: list[ApprovalModel],
    events: list[AgentEventModel], plan: VerificationPlan,
) -> bool:
    if [event.sequence for event in events] != list(range(1, len(events) + 1)):
        return False
    preliminary = [event for event in events if event.event_type == "agent_result_recorded"]
    expected_result = {"schema_version": 1, "result_status": "ready_for_verification"}
    if len(preliminary) != 1 or metadata_digest(preliminary[0].payload) != metadata_digest(expected_result):
        return False
    result_sequence = preliminary[0].sequence
    status = AgentRunStatus.QUEUED
    attempts: set[UUID] = set()
    for event in events:
        required_status = _EVENT_RUN_STATUS.get(event.event_type)
        if required_status is not None and status is not required_status:
            return False
        if (
            event.event_type.startswith("tool_call_") and event.event_type not in _TOOL_EVENTS
            or event.event_type.startswith("approval_") and event.event_type not in _APPROVAL_EVENTS
        ):
            return False
        if event.event_type == VERIFICATION_RECORDED:
            previous = decode_evidence(event.payload)
            if (
                previous.plan.binding != plan.binding or previous.attempt_id in attempts
                or previous.verdict is not VerificationVerdict.INCONCLUSIVE
            ):
                return False
            attempts.add(previous.attempt_id)
        if event.event_type == "agent_execution_failed":
            return False
        if event.event_type == "agent_run_status_changed":
            if event.payload.keys() != {"from", "to"} or event.payload["from"] != status.value:
                return False
            target = AgentRunStatus(event.payload["to"])
            validate_agent_run_transition(status, target)
            if target is AgentRunStatus.VERIFYING and event.sequence <= result_sequence:
                return False
            status = target
    if status is not AgentRunStatus.VERIFYING:
        return False
    if sum(event.event_type in _TOOL_EVENTS for event in events) != 3 * len(calls):
        return False
    if sum(event.event_type in _APPROVAL_EVENTS for event in events) != 2 * len(approvals):
        return False
    write_ids = {call.id for call in calls if call.tool_name == "apply_patch"}
    if {item.tool_call_id for item in approvals} != write_ids or len(approvals) != len(write_ids):
        return False
    for call in calls:
        if call.status != ToolCallStatus.SUCCEEDED.value or call.tool_name not in _READ_TOOLS | {"apply_patch"}:
            return False
        lifecycle = [event for event in events if event.event_type in _TOOL_EVENTS
                     and event.payload.get("tool_call_id") == str(call.id)]
        expected = [
            encode_tool_call_event(ToolCallAudit(call.id, call.tool_name, 1, status))
            for status in (ToolCallStatus.PROPOSED, ToolCallStatus.RUNNING, ToolCallStatus.SUCCEEDED)
        ]
        if len(lifecycle) != 3 or lifecycle[-1].sequence >= result_sequence:
            return False
        for event, (kind, payload) in zip(lifecycle, expected, strict=True):
            actual = dict(event.payload)
            if "duration_ms" in actual:
                duration = actual.pop("duration_ms")
                if kind != "tool_call_finished" or type(duration) is not int or duration < 0:
                    return False
            if event.event_type != kind or metadata_digest(actual) != metadata_digest(payload):
                return False
        if call.tool_name == "apply_patch" and not _complete_approval(call, approvals, events, lifecycle, plan):
            return False
    return True


def _complete_approval(
    call: ToolCallModel, approvals: list[ApprovalModel], events: list[AgentEventModel],
    lifecycle: list[AgentEventModel], plan: VerificationPlan,
) -> bool:
    approval = next(item for item in approvals if item.tool_call_id == call.id)
    if approval.status != ApprovalStatus.APPROVED.value or approval.decided_at is None:
        return False
    related = [event for event in events if event.event_type in _APPROVAL_EVENTS
               and event.payload.get("approval_id") == str(approval.id)]
    if len(related) != 2:
        return False
    request, decision = related
    payload = request.payload
    reference = ApprovalReference(
        UUID(str(approval.id)),
        ToolCallTicket(UUID(str(call.id)), plan.binding.run_id, plan.binding.workspace_id, call.tool_name, 1),
        payload["patch_sha256"], payload["sources_sha256"],
    )
    return (
        request.event_type == "approval_requested"
        and metadata_digest(payload) == metadata_digest(reference.requested_payload())
        and decision.event_type == "approval_decided"
        and metadata_digest(decision.payload) == metadata_digest({
            "schema_version": 1, "approval_id": str(approval.id),
            "tool_call_id": str(call.id), "decision": "approved",
        })
        and lifecycle[0].sequence < request.sequence < decision.sequence < lifecycle[1].sequence
    )
