"""Read and validate stored AgentRun data before projecting it to HTTP DTOs."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel, AgentRunModel
from app.domain.execution import AgentRunStatus
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.schemas.agent_run import (
    AgentRunEventsResponse,
    AgentRunPublicEvent,
    AgentRunResultResponse,
    AgentRunUsageResponse,
    EmptyEventDetails,
    RunFailureDetails,
    RunFailureEvent,
    RunMetadataEvent,
    RunPreliminaryDetails,
    RunPreliminaryEvent,
    RunStepDetails,
    RunStepEvent,
    RunTransitionDetails,
    RunTransitionEvent,
    RunVerificationDetails,
    RunVerificationEvent,
)
from app.services.agent_failure import AGENT_EXECUTION_FAILED_EVENT, decode_agent_failure
from app.services.agent_result_codec import RESULT_SCHEMA_VERSION, decode_agent_result
from app.services.agent_runs import AgentRunNotFoundError
from app.services.agent_usage_codec import AGENT_USAGE_RECORDED_EVENT, AgentUsageRecord, decode_agent_usage
from app.sandbox.events import AGENT_STEP_EVENT
from app.sandbox.verification import SANDBOX_VERIFICATION_RECORDED, SandboxVerification
from app.core.digest import metadata_digest
from app.domain.verification import VERIFICATION_RECORDED, VerificationVerdict


_RESULT_KINDS = (AGENT_EXECUTION_FAILED_EVENT, AGENT_USAGE_RECORDED_EVENT, SANDBOX_VERIFICATION_RECORDED)
MAX_RESULT_EVENTS = 512


class InvalidAgentRunReadError(ValueError):
    """A stored record could not be safely projected."""


class AgentRunReadService:
    def __init__(self, session: AsyncSession) -> None:
        self._runs = AgentRunRepository(session)
        self._events = AgentEventRepository(session)

    async def read_events(self, run_id: UUID, after_sequence: int, limit: int) -> AgentRunEventsResponse:
        if await self._runs.get(run_id) is None:
            raise AgentRunNotFoundError(run_id)
        records = await self._events.read_after(run_id, after_sequence, limit=limit + 1)
        try:
            items = [_project_event(run_id, event) for event in records[:limit]]
            return AgentRunEventsResponse(
                items=items, next_after_sequence=items[-1].sequence if items else after_sequence,
                has_more=len(records) > limit,
            )
        except (ValueError, TypeError, KeyError):
            raise InvalidAgentRunReadError() from None

    async def read_result(self, run_id: UUID) -> AgentRunResultResponse:
        """Validate the complete bounded snapshot before the API projects it."""
        snapshot = await self._events.read_result_snapshot(
            run_id, event_types=_RESULT_KINDS, limit=MAX_RESULT_EVENTS + 1,
        )
        if snapshot is None:
            raise AgentRunNotFoundError(run_id)
        run, _, events = snapshot
        if len(events) > MAX_RESULT_EVENTS:
            raise InvalidAgentRunReadError()
        try:
            return _validate_result(run, events)
        except (ValueError, TypeError, KeyError):
            raise InvalidAgentRunReadError() from None


def _versioned(payload: object, version: int) -> dict:
    if type(payload) is not dict or type(payload.get("schema_version")) is not int or payload["schema_version"] != version:
        raise ValueError("invalid_event_version")
    return payload


def _uuid(value: str) -> UUID:
    parsed = UUID(value) if type(value) is str else None
    if parsed is None or str(parsed) != value:
        raise ValueError("invalid_event_id")
    return parsed


def _project_event(run_id: UUID, event: AgentEventModel) -> AgentRunPublicEvent:
    kind, payload = event.event_type, event.payload
    common = {"sequence": event.sequence, "created_at": event.created_at}
    if kind == "agent_run_status_changed":
        if type(payload) is not dict or payload.keys() != {"from", "to"}:
            raise ValueError("invalid_transition_event")
        return RunTransitionEvent(kind=kind, details=RunTransitionDetails(
            previous=AgentRunStatus(payload["from"]), to=AgentRunStatus(payload["to"]),
        ), **common)
    if kind == "agent_result_recorded":
        body = _versioned(payload, RESULT_SCHEMA_VERSION)
        if body.keys() != {"schema_version", "result_status"}:
            raise ValueError("invalid_result_event")
        return RunPreliminaryEvent(kind=kind, details=RunPreliminaryDetails(preliminary_status=body["result_status"]),
                                   **common)
    if kind == AGENT_EXECUTION_FAILED_EVENT:
        return RunFailureEvent(kind=kind, details=RunFailureDetails(code=decode_agent_failure(payload).code), **common)
    if kind == AGENT_STEP_EVENT:
        body = _versioned(payload, 1)
        extra = {k: body[k] for k in ("turn", "tools", "tool", "target", "ok", "exit_code", "outcome") if k in body}
        return RunStepEvent(kind=kind, details=RunStepDetails(step=body["step"], summary=body["summary"], **extra),
                            **common)
    if kind == SANDBOX_VERIFICATION_RECORDED:
        return RunVerificationEvent(kind=VERIFICATION_RECORDED, details=RunVerificationDetails(
            verdict=SandboxVerification.from_payload(payload).verdict), **common)

    if kind == AGENT_USAGE_RECORDED_EVENT:
        decode_agent_usage(run_id, payload)
    else:
        kind = "other"      # includes M1-era tool_call_* / approval_* / verification events of old runs
    return RunMetadataEvent(kind=kind, details=EmptyEventDetails(), **common)


def _project_usage(record: AgentUsageRecord) -> AgentRunUsageResponse:
    estimate = record.cost_estimate
    return AgentRunUsageResponse(
        known_total_tokens=record.snapshot.known_total_tokens,
        usage_complete=record.snapshot.usage_complete,
        known_cost_usd=format(estimate.known_cost_usd, "f") if estimate.priced_calls else None,
        cost_complete=estimate.cost_complete,
    )


def _validate_result(run: AgentRunModel, events: list) -> AgentRunResultResponse:
    status = AgentRunStatus(run.status)
    preliminary = None if run.final_result is None else decode_agent_result(run.final_result).status
    failure = None
    usage = None
    verdict = None
    result_digest = metadata_digest(run.final_result) if preliminary == "ready_for_verification" else None
    for _sequence, kind, payload in events:
        if kind == AGENT_EXECUTION_FAILED_EVENT:
            if failure is not None:
                raise ValueError("duplicate_execution_failure")
            failure = decode_agent_failure(payload).code
        elif kind == AGENT_USAGE_RECORDED_EVENT:
            if usage is not None:
                raise ValueError("duplicate_agent_usage")
            usage = _project_usage(decode_agent_usage(run.id, payload))
        elif kind == SANDBOX_VERIFICATION_RECORDED:
            sandbox_evidence = SandboxVerification.from_payload(payload)
            if (
                verdict is not None or result_digest is None
                or payload.get("result_sha256") != result_digest
            ):
                raise ValueError("inconsistent_sandbox_verification_history")
            verdict = sandbox_evidence.verdict

    if (
        (status in (AgentRunStatus.VERIFYING, AgentRunStatus.SUCCEEDED) and preliminary != "ready_for_verification")
        or (preliminary == "blocked" and status is not AgentRunStatus.FAILED)
        or (preliminary is not None and status in (
            AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_APPROVAL,
        ))
        or (failure is not None and (status is not AgentRunStatus.FAILED or preliminary is not None))
        or ((verdict is VerificationVerdict.PASS) != (status is AgentRunStatus.SUCCEEDED))
        or (verdict is VerificationVerdict.FAIL and status is not AgentRunStatus.FAILED)
    ):
        raise ValueError("inconsistent_agent_run_result")

    return AgentRunResultResponse(
        run_id=run.id, run_status=status, preliminary_status=preliminary, execution_failure_code=failure,
        verification_verdict=verdict, usage=usage,
    )
