from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

from app.tools.contracts import ToolInput, ToolOutput, ToolSpec


ToolHandler: TypeAlias = Callable[[ToolInput], Awaitable[ToolOutput]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class DuplicateToolError(ValueError):
    """A tool name has more than one definition."""


class UnknownToolError(LookupError):
    """No tool is registered under the requested name."""


class ToolVersionMismatchError(LookupError):
    """The requested version differs from the registered version."""


class ToolRegistry:
    """Read-only catalog of trusted tool definitions."""

    def __init__(self, definitions: Iterable[RegisteredTool]) -> None:
        tools: dict[str, RegisteredTool] = {}

        for definition in definitions:
            if not isinstance(definition, RegisteredTool):
                raise TypeError("Tool definitions must be RegisteredTool instances")
            if not isinstance(definition.spec, ToolSpec):
                raise TypeError("Registered tool spec must be a ToolSpec instance")
            if not callable(definition.handler):
                raise TypeError("Registered tool handler must be callable")

            # Validate field values again because model_copy(update=...) bypasses validation.
            fields = {name: getattr(definition.spec, name) for name in ToolSpec.model_fields}
            spec = ToolSpec.model_validate(fields)
            schemas = (("input_schema", spec.input_schema), ("output_schema", spec.output_schema))
            for field, schema in schemas:
                config = schema.model_config
                if (
                    config.get("extra") != "forbid"
                    or config.get("strict") is not True
                    or config.get("frozen") is not True
                ):
                    raise ValueError(f"{field} must retain extra='forbid', strict=True and frozen=True")

            if spec.name in tools:
                raise DuplicateToolError(f"Tool '{spec.name}' is already registered")
            tools[spec.name] = RegisteredTool(spec=spec, handler=definition.handler)

        self._tools = MappingProxyType(tools)

    def get(self, name: str, *, version: int | None = None) -> RegisteredTool:
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int):
                raise TypeError("Tool version must be an integer")
            if version < 1:
                raise ValueError("Tool version must be positive")

        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError("Tool name is not registered")
        if version is not None and version != tool.spec.version:
            raise ToolVersionMismatchError(
                f"Tool '{tool.spec.name}' does not support the requested version"
            )
        return tool

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in sorted(self._tools))
