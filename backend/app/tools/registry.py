from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from inspect import isasyncgenfunction, iscoroutinefunction
from types import MappingProxyType
from typing import TypeAlias

from app.domain.capabilities import Capability
from app.tools.contracts import ToolInput, ToolOutput, ToolSpec


ToolHandler: TypeAlias = Callable[[ToolInput], Awaitable[ToolOutput]]
PathSelector: TypeAlias = Callable[[ToolInput], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    path_selector: PathSelector | None = None
    synthetic: bool = False


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

            selector = definition.path_selector
            if selector is not None:
                if not callable(selector) or any(
                    iscoroutinefunction(fn) or isasyncgenfunction(fn)
                    for fn in (selector, type(selector).__call__)
                ):
                    raise TypeError("Tool path selector must be a synchronous callable")
            if type(definition.synthetic) is not bool:
                raise TypeError("Tool synthetic flag must be a bool")
            if definition.synthetic and selector is not None:
                raise ValueError("Synthetic tools cannot declare a path selector")
            if definition.synthetic and (
                spec.capability is not Capability.REPOSITORY_READ or spec.side_effect
            ):
                raise ValueError("Synthetic tools must be read-only")

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
            tools[spec.name] = RegisteredTool(
                spec=spec,
                handler=definition.handler,
                path_selector=selector,
                synthetic=definition.synthetic,
            )

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
