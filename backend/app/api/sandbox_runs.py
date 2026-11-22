"""Human-oriented read endpoints of ForgeMini: one call that answers "what happened and why?"."""
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.api.errors import ApiError
from app.db.models import AgentEventModel, AgentRunModel, TaskModel
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.verification import SANDBOX_VERIFICATION_RECORDED, SandboxVerification
from app.services.agent_failure import AGENT_EXECUTION_FAILED_EVENT, decode_agent_failure
from app.services.agent_result_codec import decode_agent_result

router = APIRouter()


class CommandRunView(BaseModel):
    exit_code: int
    timed_out: bool
    output_tail: str


class VerificationView(BaseModel):
    verdict: str
    reason: str
    test_command: str
    image: str
    changed_paths: list[str]
    protected_violations: list[str]
    patched: CommandRunView | None
    baseline: CommandRunView | None
    duration_ms: int


class UsageView(BaseModel):
    """What the agent loop consumed. No money value on purpose: prices depend on the model and change."""
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


class RunReport(BaseModel):
    run_id: UUID
    task_id: UUID
    run_status: str
    task_status: str
    goal: str
    agent_summary: str | None          # what the agent says it did / why it stopped
    blocked_reason: str | None
    failure_code: str | None           # infrastructure/provider failure, if the run never finished
    verification: VerificationView | None
    has_patch: bool
    usage: UsageView | None = None     # None when the run never produced an outcome (e.g. provider failure)


def _store(request: Request) -> FileArtifactStore | None:
    return getattr(request.app.state, "artifact_store", None)


@router.get("/agent-runs/{run_id}/report", response_model=RunReport)
async def get_run_report(run_id: UUID, session: Annotated[AsyncSession, Depends(get_session)],
                         request: Request) -> RunReport:
    run = await session.get(AgentRunModel, run_id)
    if run is None:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "AgentRun not found")
    task = await session.get(TaskModel, run.task_id)
    summary = blocked = None
    if run.final_result is not None:
        result = decode_agent_result(run.final_result)
        summary, blocked = result.summary, result.blocked_reason
    events = (await session.scalars(select(AgentEventModel).where(
        AgentEventModel.agent_run_id == run_id,
        AgentEventModel.event_type.in_((SANDBOX_VERIFICATION_RECORDED, AGENT_EXECUTION_FAILED_EVENT)),
    ).order_by(AgentEventModel.sequence))).all()
    failure = next((decode_agent_failure(e.payload).code.value for e in events
                    if e.event_type == AGENT_EXECUTION_FAILED_EVENT), None)
    verification = next((SandboxVerification.from_payload(e.payload) for e in reversed(events)
                         if e.event_type == SANDBOX_VERIFICATION_RECORDED), None)
    store = _store(request)
    usage = None
    if store and store.has_run(run_id):
        outcome = store.load_outcome(run_id)
        usage = UsageView(**{k: int(outcome.get(k, 0)) for k in ("steps", "tool_calls", "input_tokens", "output_tokens")})
    return RunReport(
        run_id=run.id, task_id=run.task_id, run_status=run.status, task_status=task.status if task else "unknown",
        goal=task.goal if task else "", agent_summary=summary, blocked_reason=blocked, failure_code=failure,
        verification=_view(verification), has_patch=bool(store and store.has_patch(run_id)), usage=usage,
    )


@router.get("/agent-runs/{run_id}/patch", response_class=PlainTextResponse)
async def get_run_patch(run_id: UUID, request: Request) -> str:
    store = _store(request)
    if store is None or not store.has_patch(run_id):
        raise ApiError(404, "PATCH_NOT_FOUND", "The agent made no changes in this run")
    return store.patch_text(run_id)


def _view(v: SandboxVerification | None) -> VerificationView | None:
    if v is None:
        return None

    def cmd(c: Any) -> CommandRunView | None:
        return CommandRunView(exit_code=c.exit_code, timed_out=c.timed_out, output_tail=c.output_tail) if c else None
    return VerificationView(
        verdict=v.verdict.value, reason=v.reason, test_command=v.test_command, image=v.image,
        changed_paths=list(v.changed_paths), protected_violations=list(v.protected_violations),
        patched=cmd(v.patched), baseline=cmd(v.baseline), duration_ms=v.duration_ms)
