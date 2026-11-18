import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentEventModel, AgentRunModel, ToolCallModel
from app.domain.execution import AgentRunStatus, ToolCallStatus
from app.domain.tool_call_transitions import InvalidToolCallTransition, validate_tool_call_transition
from app.permissions.context import PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services.tool_call_audit import (
    ToolCallAudit,
    ToolCallAuditError,
    ToolCallErrorCode,
    encode_tool_call_event,
)
from app.tools.contracts import ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError


async def _finish_interrupted_proposal(cleanup: Awaitable[None], error: BaseException) -> None:
    # Повторная отмена прерывает cleanup, но исходное исключение остаётся основным.
    try:
        async with asyncio.timeout(2):
            await cleanup
    except (asyncio.CancelledError, Exception):
        error.add_note("tool_call_audit_finalization_failed")


@dataclass(frozen=True, slots=True)
class ToolCallTicket:
    tool_call_id: UUID
    run_id: UUID
    workspace_id: UUID
    tool_name: str
    tool_version: int


class ToolCallService:
    """Own one session and transaction per audit lifecycle operation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def propose(self, context: PermissionContext, spec: ToolSpec) -> ToolCallTicket:
        if type(context) is not PermissionContext:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)
        ticket = ToolCallTicket(uuid4(), context.run_id, context.workspace_id, spec.name, spec.version)
        commit_attempted = False
        try:
            async with tool_call_transaction(self._session_factory) as session:
                await lock_tool_call_run(session, ticket, require_running=True)
                await stage_tool_call_proposal(session, ticket)
                commit_attempted = True
        except (asyncio.CancelledError, ToolCallAuditError) as error:
            if commit_attempted:
                code = ToolCallErrorCode.CANCELLED if isinstance(error, asyncio.CancelledError) else error.code
                await _finish_interrupted_proposal(self._reconcile_proposal(ticket, code), error)
            raise
        return ticket

    async def _reconcile_proposal(self, ticket: ToolCallTicket, code: ToolCallErrorCode) -> None:
        async with tool_call_transaction(self._session_factory) as session:
            await lock_tool_call_run(session, ticket, require_running=False)
            call = await lock_proposed_tool_call(session, ticket)
            if call is not None:
                await transition_locked_tool_call(session, ticket, call, ToolCallStatus.FAILED, error_code=code)

    async def start(self, ticket: ToolCallTicket) -> None:
        await self._transition(ticket, ToolCallStatus.RUNNING)

    async def finish(
        self,
        ticket: ToolCallTicket,
        status: ToolCallStatus,
        *,
        error_code: ToolCallErrorCode | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if not isinstance(status, ToolCallStatus) or status in (ToolCallStatus.PROPOSED, ToolCallStatus.RUNNING):
            raise ValueError("tool_call_terminal_status_required")
        await self._transition(ticket, status, error_code=error_code, duration_ms=duration_ms)

    async def _transition(
        self,
        ticket: ToolCallTicket,
        status: ToolCallStatus,
        *,
        error_code: ToolCallErrorCode | None = None,
        duration_ms: int | None = None,
    ) -> None:
        async with tool_call_transaction(self._session_factory) as session:
            # AgentEventRepository also locks the parent; it is already held here.
            await lock_tool_call_run(
                session, ticket, require_running=status is ToolCallStatus.RUNNING,
            )
            call = await ToolCallRepository(session).get_for_update(ticket.run_id, ticket.tool_call_id)
            if call is None:
                raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED)
            await transition_locked_tool_call(
                session, ticket, call, status, error_code=error_code, duration_ms=duration_ms,
            )


@asynccontextmanager
async def tool_call_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open one audit transaction for the calling lifecycle phase."""
    try:
        session = session_factory()
        primary: BaseException | None = None
        try:
            yield session
            await session.commit()
        except BaseException as error:
            primary = error
            try:
                await session.rollback()
            except Exception:
                error.add_note("tool_call_audit_rollback_failed")
            raise
        finally:
            try:
                await session.close()
            except Exception:
                if primary is None:
                    raise
                primary.add_note("tool_call_audit_session_close_failed")
    except (ToolCallAuditError, ToolExecutionError, InvalidToolCallTransition):
        raise
    except Exception:
        raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED) from None


async def lock_tool_call_run(
    session: AsyncSession, ticket: ToolCallTicket, *, require_running: bool,
) -> AgentRunModel:
    """Lock the parent first and check the ticket workspace before touching a ToolCall."""
    run = await AgentRunRepository(session).get_for_update(ticket.run_id)
    if run is None:
        raise ToolCallAuditError(ToolCallErrorCode.RUN_NOT_RUNNING)
    task = await TaskRepository(session).get(run.task_id)
    if task is None or task.workspace_id != ticket.workspace_id:
        raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)
    if require_running and run.status != AgentRunStatus.RUNNING.value:
        raise ToolCallAuditError(ToolCallErrorCode.RUN_NOT_RUNNING)
    return run


async def lock_proposed_tool_call(session: AsyncSession, ticket: ToolCallTicket) -> ToolCallModel | None:
    """Проверить proposal под уже взятой блокировкой AgentRun."""
    call = await ToolCallRepository(session).get_for_update(ticket.run_id, ticket.tool_call_id)
    if call is None or call.status != ToolCallStatus.PROPOSED.value:
        return None
    expected = encode_tool_call_event(
        ToolCallAudit(ticket.tool_call_id, ticket.tool_name, ticket.tool_version, ToolCallStatus.PROPOSED),
    )
    statement = select(AgentEventModel.event_type, AgentEventModel.payload).where(
        AgentEventModel.agent_run_id == ticket.run_id,
        AgentEventModel.payload["tool_call_id"].astext == str(ticket.tool_call_id),
        AgentEventModel.event_type.in_(("tool_call_proposed", "tool_call_started", "tool_call_finished")),
    ).limit(2)
    if call.tool_name != ticket.tool_name or (await session.execute(statement)).all() != [expected]:
        raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED)
    return call


async def stage_tool_call_proposal(session: AsyncSession, ticket: ToolCallTicket) -> None:
    """Stage the row and audit event after locking the ticket's parent AgentRun."""
    ToolCallRepository(session).add_proposed(ticket.run_id, ticket.tool_call_id, ticket.tool_name)
    await _append_tool_call_event(session, ticket, ToolCallStatus.PROPOSED)


async def transition_locked_tool_call(
    session: AsyncSession,
    ticket: ToolCallTicket,
    locked_call: ToolCallModel,
    status: ToolCallStatus,
    *,
    error_code: ToolCallErrorCode | None = None,
    duration_ms: int | None = None,
) -> None:
    """Stage status and audit in the caller's session.

    The caller has validated the workspace and holds the AgentRun and ToolCall
    locks in that order. It owns commit, rollback and close.
    """
    if (locked_call.id, locked_call.agent_run_id, locked_call.tool_name) != (
        ticket.tool_call_id, ticket.run_id, ticket.tool_name,
    ):
        raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED)
    current = ToolCallStatus(locked_call.status)
    validate_tool_call_transition(current, status)
    if duration_ms is not None and current is not ToolCallStatus.RUNNING:
        raise ValueError("tool_call_duration_requires_start")
    locked_call.status = status.value
    await _append_tool_call_event(session, ticket, status, error_code=error_code, duration_ms=duration_ms)


async def _append_tool_call_event(
    session: AsyncSession,
    ticket: ToolCallTicket,
    status: ToolCallStatus,
    *,
    error_code: ToolCallErrorCode | None = None,
    duration_ms: int | None = None,
) -> None:
    audit = ToolCallAudit(
        ticket.tool_call_id, ticket.tool_name, ticket.tool_version, status, error_code, duration_ms,
    )
    event_type, payload = encode_tool_call_event(audit)
    await AgentEventRepository(session).append(ticket.run_id, event_type=event_type, payload=payload)
