import asyncio
from time import perf_counter_ns

from app.domain.execution import ToolCallStatus
from app.domain.tool_call_transitions import InvalidToolCallTransition
from app.permissions.context import PermissionContext
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import ToolCallService, ToolCallTicket
from app.tools.contracts import ToolOutput
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor


class AuditedToolExecutor:
    """Record durable admission and observed outcomes around a standalone executor."""

    def __init__(self, executor: ToolExecutor, tool_calls: ToolCallService) -> None:
        self._executor = executor
        self._tool_calls = tool_calls

    async def execute(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        version: int | None = None,
        context: PermissionContext | None = None,
    ) -> ToolOutput:
        if not isinstance(arguments, dict):
            raise ToolExecutionError(ToolErrorCode.INVALID_REQUEST)
        try:
            spec = self._executor.get_tool_spec(name, version=version)
        except ToolExecutionError as error:
            if error.code is not ToolErrorCode.VERSION_MISMATCH:
                raise
            # Record the canonical registered version. Execution still sees the requested version.
            spec = self._executor.get_tool_spec(name)
        if type(context) is not PermissionContext:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)

        ticket = await self._tool_calls.propose(context, spec)
        started_at_ns = None

        async def start() -> None:
            nonlocal started_at_ns
            try:
                await self._tool_calls.start(ticket)
            except InvalidToolCallTransition:
                raise ToolCallAuditError(ToolCallErrorCode.AUDIT_PERSISTENCE_FAILED) from None
            started_at_ns = perf_counter_ns()

        try:
            output = await self._executor.execute(
                name, arguments, version=version, context=context, before_handler=start,
            )
        except asyncio.CancelledError as error:
            await self._finish_error(ticket, ToolCallErrorCode.CANCELLED, started_at_ns, error)
            raise
        except (ToolExecutionError, ToolCallAuditError) as error:
            await self._finish_error(ticket, ToolCallErrorCode(error.code.value), started_at_ns, error)
            raise

        await self._tool_calls.finish(
            ticket, ToolCallStatus.SUCCEEDED, duration_ms=_duration_ms(started_at_ns),
        )
        return output

    async def _finish_error(
        self,
        ticket: ToolCallTicket,
        code: ToolCallErrorCode,
        started_at_ns: int | None,
        error: BaseException,
    ) -> None:
        status = ToolCallStatus.FAILED
        if started_at_ns is None and code in (ToolCallErrorCode.PERMISSION_DENIED, ToolCallErrorCode.EXECUTION_BLOCKED):
            status = ToolCallStatus.DENIED
        try:
            await self._tool_calls.finish(ticket, status, error_code=code, duration_ms=_duration_ms(started_at_ns))
        except Exception:
            error.add_note("tool_call_audit_finalization_failed")


def _duration_ms(started_at_ns: int | None) -> int | None:
    if started_at_ns is None:
        return None
    return (perf_counter_ns() - started_at_ns) // 1_000_000
