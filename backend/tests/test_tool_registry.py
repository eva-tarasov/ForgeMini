from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ConfigDict, ValidationError

from app.domain.capabilities import Capability
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.registry import (
    DuplicateToolError,
    RegisteredTool,
    ToolRegistry,
    ToolVersionMismatchError,
    UnknownToolError,
)


class SampleInput(ToolInput):
    label: str


class SampleOutput(ToolOutput):
    accepted: bool


async def fake_handler(_tool_input: ToolInput) -> ToolOutput:
    return SampleOutput(accepted=True)


def make_tool(**overrides: object) -> RegisteredTool:
    fields = {
        "name": "sample_read",
        "version": 1,
        "description": "A synthetic tool definition for registry tests.",
        "input_schema": SampleInput,
        "output_schema": SampleOutput,
        "capability": Capability.REPOSITORY_READ,
        "risk_level": ToolRiskLevel.LOW,
        "timeout_ms": 1000,
        "side_effect": False,
        "requires_approval": False,
    }
    return RegisteredTool(spec=ToolSpec.model_validate(fields | overrides), handler=fake_handler)


def test_registry_get_returns_registered_tool() -> None:
    definition = make_tool(requires_approval=True)
    registry = ToolRegistry([definition])

    registered = registry.get("sample_read")

    assert registered.spec == definition.spec
    assert registered.spec is not definition.spec
    assert registered.spec.requires_approval is True
    assert registered.handler is definition.handler
    assert registry.get("sample_read", version=1) is registered


def test_registry_accepts_multiple_tools_from_an_iterable() -> None:
    first = make_tool(name="sample_one")
    second = make_tool(name="sample_two", version=2)
    registry = ToolRegistry(iter((first, second)))

    assert registry.get("sample_one").spec == first.spec
    assert registry.get("sample_two", version=2).spec == second.spec


def test_empty_registry_has_empty_discovery() -> None:
    registry = ToolRegistry([])

    assert registry.list_specs() == ()
    with pytest.raises(UnknownToolError):
        registry.get("sample_read")


@pytest.mark.parametrize("name", ["missing", "SAMPLE_READ", "sample_read "])
def test_registry_requires_an_exact_name(name: str) -> None:
    registry = ToolRegistry([make_tool()])

    with pytest.raises(UnknownToolError):
        registry.get(name, version=1)


def test_registry_rejects_version_mismatch() -> None:
    registry = ToolRegistry([make_tool(version=2)])

    with pytest.raises(ToolVersionMismatchError, match="sample_read"):
        registry.get("sample_read", version=1)


@pytest.mark.parametrize(
    ("version", "error"),
    [
        (True, TypeError),
        (False, TypeError),
        ("1", TypeError),
        (1.0, TypeError),
        (0, ValueError),
        (-1, ValueError),
    ],
)
def test_registry_rejects_invalid_version_argument(version: object, error: type[Exception]) -> None:
    registry = ToolRegistry([make_tool()])

    with pytest.raises(error, match="Tool version"):
        registry.get("sample_read", version=version)


@pytest.mark.parametrize("second_version", [1, 2])
def test_duplicate_tool_name_is_rejected(second_version: int) -> None:
    first = make_tool(version=1)
    second = make_tool(version=second_version)

    with pytest.raises(DuplicateToolError, match="sample_read"):
        ToolRegistry([first, second])


def test_registry_rejects_invalid_definition() -> None:
    with pytest.raises(TypeError, match="RegisteredTool instances"):
        ToolRegistry([make_tool().spec])


def test_registry_rejects_invalid_descriptor() -> None:
    definition = replace(make_tool(), spec={"name": "sample_read"})

    with pytest.raises(TypeError, match="ToolSpec instance"):
        ToolRegistry([definition])


def test_registry_rejects_non_callable_handler() -> None:
    definition = replace(make_tool(), handler=object())

    with pytest.raises(TypeError, match="handler must be callable"):
        ToolRegistry([definition])


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"requires_approval": False}, "requires_approval=True"),
        ({"timeout_ms": 30_001}, "timeout_ms"),
    ],
)
def test_registry_rejects_invalid_copied_spec(updates: dict[str, object], message: str) -> None:
    definition = make_tool(
        name="sample_write",
        capability=Capability.REPOSITORY_WRITE,
        risk_level=ToolRiskLevel.HIGH,
        side_effect=True,
        requires_approval=True,
    )
    copied_spec = definition.spec.model_copy(update=updates)

    with pytest.raises(ValidationError, match=message):
        ToolRegistry([replace(definition, spec=copied_spec)])


@pytest.mark.parametrize("field", ["input_schema", "output_schema"])
@pytest.mark.parametrize(
    "config",
    [
        pytest.param(ConfigDict(extra="ignore"), id="extra-ignore"),
        pytest.param(ConfigDict(strict=False), id="strict-false"),
        pytest.param(ConfigDict(frozen=False), id="frozen-false"),
    ],
)
def test_registry_rejects_weakened_schema_configuration(field: str, config: ConfigDict) -> None:
    base = SampleInput if field == "input_schema" else SampleOutput

    class WeakenedSchema(base):
        model_config = config

    definition = make_tool(**{field: WeakenedSchema})

    with pytest.raises(ValueError, match=f"{field} must retain"):
        ToolRegistry([definition])


def test_discovery_returns_only_specs_in_name_order() -> None:
    zulu = make_tool(name="zulu")
    alpha = make_tool(name="alpha")
    middle = make_tool(name="middle")
    registry = ToolRegistry([zulu, alpha, middle])

    specs = registry.list_specs()

    assert isinstance(specs, tuple)
    assert specs == (alpha.spec, middle.spec, zulu.spec)


def test_registry_is_independent_of_caller_list_mutation() -> None:
    original = make_tool()
    definitions = [original]
    registry = ToolRegistry(definitions)

    definitions[:] = [make_tool(name="replacement")]

    assert registry.list_specs() == (original.spec,)
    with pytest.raises(UnknownToolError):
        registry.get("replacement")


def test_registered_tool_is_frozen_and_slotted() -> None:
    registered = ToolRegistry([make_tool()]).get("sample_read")

    assert not hasattr(registered, "__dict__")
    with pytest.raises(FrozenInstanceError):
        registered.spec = make_tool(name="replacement").spec


def test_discovery_preserves_schema_class_identity() -> None:
    registry = ToolRegistry([make_tool()])
    (spec,) = registry.list_specs()

    assert spec.input_schema is SampleInput
    assert spec.output_schema is SampleOutput
    assert spec.input_schema.model_json_schema()["properties"]["label"]["type"] == "string"
    assert spec.output_schema.model_json_schema()["properties"]["accepted"]["type"] == "boolean"


def test_registry_does_not_invoke_handler() -> None:
    handler = AsyncMock(spec=fake_handler)
    definition = replace(make_tool(), handler=handler)

    registry = ToolRegistry([definition])
    registered = registry.get("sample_read")
    specs = registry.list_specs()

    assert registered.handler is handler
    assert specs == (registered.spec,)
    handler.assert_not_called()
