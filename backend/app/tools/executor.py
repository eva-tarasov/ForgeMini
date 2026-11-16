import asyncio
from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction

from pydantic import ValidationError

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.permissions.policy import PermissionOutcome, PermissionPolicy
from app.tools.contracts import ToolOutput, ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.registry import RegisteredTool, ToolRegistry, ToolVersionMismatchError, UnknownToolError


class ToolExecutor:
    """Authorize and execute trusted tools with the read-only gate."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._permission_policy = PermissionPolicy()

    def get_tool_spec(self, name: str, *, version: int | None = None) -> ToolSpec:
        """Look up a registered descriptor without authorizing or executing the tool."""
        return self._resolve_tool(name, version).spec

    def _resolve_tool(self, name: str, version: int | None) -> RegisteredTool:
        if not isinstance(name, str) or not name:
            raise ToolExecutionError(ToolErrorCode.INVALID_REQUEST)
        try:
            return self._registry.get(name, version=version)
        except UnknownToolError:
            raise ToolExecutionError(ToolErrorCode.UNKNOWN_TOOL) from None
        except ToolVersionMismatchError:
            raise ToolExecutionError(ToolErrorCode.VERSION_MISMATCH) from None
        except (TypeError, ValueError):
            raise ToolExecutionError(ToolErrorCode.INVALID_REQUEST) from None

    async def execute(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        version: int | None = None,
        context: PermissionContext | None = None,
        before_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> ToolOutput:
        if not isinstance(arguments, dict):
            raise ToolExecutionError(ToolErrorCode.INVALID_REQUEST)
        tool = self._resolve_tool(name, version)
        spec = tool.spec
        try:
            tool_input = spec.input_schema.model_validate(arguments, strict=True)
        except ValidationError:
            raise ToolExecutionError(ToolErrorCode.INVALID_INPUT) from None
        except Exception:
            raise ToolExecutionError(ToolErrorCode.INTERNAL_ERROR) from None

        if type(context) is not PermissionContext:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)
        if tool.bound_context is not None and context is not tool.bound_context:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)

        paths = None
        if tool.path_selector is not None:
            try:
                paths = tool.path_selector(tool_input)
            except Exception:
                raise ToolExecutionError(ToolErrorCode.INTERNAL_ERROR) from None

        decision = self._permission_policy.evaluate(spec, context, paths=paths, synthetic=tool.synthetic)
        if decision.outcome is PermissionOutcome.APPROVAL_REQUIRED:
            raise ToolExecutionError(ToolErrorCode.EXECUTION_BLOCKED)
        if decision.outcome is not PermissionOutcome.ALLOW:
            raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED)

        if (
            spec.capability is not Capability.REPOSITORY_READ
            or spec.side_effect is not False
            or spec.requires_approval is not False
        ):
            raise ToolExecutionError(ToolErrorCode.EXECUTION_BLOCKED)

        handler = tool.handler
        # Python's implicit call looks up __call__ on the handler's type.
        if not (iscoroutinefunction(handler) or iscoroutinefunction(type(handler).__call__)):
            raise ToolExecutionError(ToolErrorCode.INVALID_HANDLER)

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        initial_cancellations = task.cancelling() if task is not None else 0
        if before_handler is not None:
            await before_handler()
            if task is not None and task.cancelling() > initial_cancellations:
                raise asyncio.CancelledError

        timeout = asyncio.timeout(spec.timeout_ms / 1000)
        deadline = timeout.when()

        def check_completion() -> None:
            # After exit, the timeout scope has removed its own cancellation request.
            if task is not None and task.cancelling() > initial_cancellations:
                raise asyncio.CancelledError from None
            if timeout.expired() or (deadline is not None and loop.time() >= deadline):
                raise ToolExecutionError(ToolErrorCode.TOOL_TIMEOUT) from None

        try:
            async with timeout:
                output = await handler(tool_input)
        except Exception:
            check_completion()
            raise ToolExecutionError(ToolErrorCode.HANDLER_FAILED) from None

        check_completion()

        if type(output) is not spec.output_schema or output.model_extra:
            raise ToolExecutionError(ToolErrorCode.INVALID_OUTPUT)

        try:
            # Keep all stored fields and bypass serializers that could transform values.
            return spec.output_schema.model_validate(dict(vars(output)), strict=True)
        except ValidationError:
            raise ToolExecutionError(ToolErrorCode.INVALID_OUTPUT) from None
        except Exception:
            raise ToolExecutionError(ToolErrorCode.INTERNAL_ERROR) from None
