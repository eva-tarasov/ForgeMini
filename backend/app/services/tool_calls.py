from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentRunModel
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
        async with self._transaction() as session:
            await self._lock_run(session, context.run_id, context.workspace_id, require_running=True)
            ticket = ToolCallTicket(uuid4(), context.run_id, context.workspace_id, spec.name, spec.version)
            ToolCallRepository(session).add_proposed(ticket.run_id, ticket.tool_call_id, ticket.tool_name)
            await self._append(session, ticket, ToolCallStatus.PROPOSED)
        return ticket

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

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            session = self._session_factory()
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

    @staticmethod
    async def _lock_run(
        session: AsyncSession, run_id: UUID, workspace_id: UUID, *, require_running: bool
    ) -> AgentRunModel:
        run = await AgentRunRepository(session).get_for_update(run_id)
        if run is None:
            raise ToolCallAuditError(ToolCallErrorCode.RUN_NOT_RUNNING)
        task = await TaskRepository(session).get(run.task_id)
        if task is None or task.workspace_id != workspace_id:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)
        if require_running and run.status != AgentRunStatus.RUNNING.value:
            raise ToolCallAuditError(ToolCallErrorCode.RUN_NOT_RUNNING)
        return run

    async def _transition(
        self,
        ticket: ToolCallTicket,
        status: ToolCallStatus,
        *,
        error_code: ToolCallErrorCode | None = None,
        duration_ms: int | None = None,
    ) -> None:
        async with self._transaction() as session:
            # AgentEventRepository also locks the parent; it is already held here.
            await self._lock_run(
                session, ticket.run_id, ticket.workspace_id, require_running=status is ToolCallStatus.RUNNING,
            )
            call = await ToolCallRepository(session).get_for_update(ticket.run_id, ticket.tool_call_id)
            if call is None or call.tool_name != ticket.tool_name:
                raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED)
            current = ToolCallStatus(call.status)
            validate_tool_call_transition(current, status)
            if duration_ms is not None and current is not ToolCallStatus.RUNNING:
                raise ValueError("tool_call_duration_requires_start")
            call.status = status.value
            await self._append(session, ticket, status, error_code=error_code, duration_ms=duration_ms)

    @staticmethod
    async def _append(
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
