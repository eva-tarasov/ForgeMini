import pytest
from pydantic import BaseModel, ValidationError

from app.domain.capabilities import Capability
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec


class SampleInput(ToolInput):
    label: str
    count: int


class SampleOutput(ToolOutput):
    accepted: bool


@pytest.fixture
def read_fields() -> dict[str, object]:
    return {
        "name": "sample_read_v1",
        "version": 1,
        "description": "Describe a synthetic read tool.",
        "input_schema": SampleInput,
        "output_schema": SampleOutput,
        "capability": Capability.REPOSITORY_READ,
        "risk_level": ToolRiskLevel.LOW,
        "timeout_ms": 1000,
        "side_effect": False,
        "requires_approval": False,
    }


@pytest.fixture
def write_fields(read_fields: dict[str, object]) -> dict[str, object]:
    return {
        **read_fields,
        "name": "sample_write_v1",
        "description": "Describe a synthetic write tool.",
        "capability": Capability.REPOSITORY_WRITE,
        "risk_level": ToolRiskLevel.HIGH,
        "side_effect": True,
        "requires_approval": True,
    }


def test_valid_read_tool_spec(read_fields: dict[str, object]) -> None:
    spec = ToolSpec(**read_fields)

    assert spec.capability is Capability.REPOSITORY_READ
    assert spec.capability.value == "repository.read"
    assert spec.risk_level is ToolRiskLevel.LOW
    assert spec.risk_level.value == "low"
    assert spec.side_effect is False
    assert spec.requires_approval is False
    assert spec.input_schema is SampleInput
    assert spec.output_schema is SampleOutput
    assert spec.audit_policy == "metadata_only"


def test_valid_write_tool_spec(write_fields: dict[str, object]) -> None:
    spec = ToolSpec(**write_fields)

    assert spec.capability is Capability.REPOSITORY_WRITE
    assert spec.capability.value == "repository.write"
    assert spec.risk_level is ToolRiskLevel.HIGH
    assert spec.risk_level.value == "high"
    assert spec.side_effect is True
    assert spec.requires_approval is True


def test_write_without_approval_is_rejected(write_fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="repository.write requires requires_approval=True"):
        ToolSpec(**(write_fields | {"requires_approval": False}))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"side_effect": False}, "repository.write requires side_effect=True"),
        ({"risk_level": ToolRiskLevel.LOW}, "repository.write requires high risk"),
    ],
)
def test_inconsistent_write_tool_spec_is_rejected(
    write_fields: dict[str, object], overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ToolSpec(**(write_fields | overrides))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"side_effect": True}, "repository.read requires side_effect=False"),
        ({"risk_level": ToolRiskLevel.HIGH}, "repository.read requires low risk"),
    ],
)
def test_inconsistent_read_tool_spec_is_rejected(
    read_fields: dict[str, object], overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ToolSpec(**(read_fields | overrides))


def test_read_tool_can_require_approval(read_fields: dict[str, object]) -> None:
    spec = ToolSpec(**(read_fields | {"requires_approval": True}))

    assert spec.requires_approval is True


@pytest.mark.parametrize(
    ("name", "description", "timeout_ms"),
    [("a", "a", 1), ("a" * 64, "a" * 500, 30_000)],
)
def test_tool_spec_accepts_field_boundaries(
    read_fields: dict[str, object], name: str, description: str, timeout_ms: int
) -> None:
    spec = ToolSpec(
        **(read_fields | {"name": name, "description": description, "timeout_ms": timeout_ms})
    )

    assert (spec.name, spec.description, spec.timeout_ms) == (name, description, timeout_ms)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "A_tool"),
        ("name", "1tool"),
        ("name", "_tool"),
        ("name", "bad-name"),
        ("name", "tool\n"),
        ("name", "a" * 65),
        ("description", ""),
        ("description", "a" * 501),
        ("version", 0),
        ("version", -1),
        ("timeout_ms", 0),
        ("timeout_ms", -1),
        ("timeout_ms", 30_001),
        ("capability", "repository.delete"),
        ("capability", "repository.read"),
        ("risk_level", "medium"),
        ("risk_level", "low"),
        ("audit_policy", "full"),
        ("extra_metadata", True),
        ("name", 42),
        ("description", b"sample"),
        ("version", True),
        ("version", "1"),
        ("timeout_ms", 1.0),
        ("side_effect", 0),
        ("requires_approval", "false"),
    ],
)
def test_invalid_tool_spec_field_is_rejected(
    read_fields: dict[str, object], field: str, value: object
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ToolSpec(**(read_fields | {field: value}))

    assert exc_info.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    ("field", "schema"),
    [
        ("input_schema", str),
        ("input_schema", BaseModel),
        ("input_schema", SampleOutput),
        ("input_schema", SampleInput(label="sample", count=1)),
        ("input_schema", {"type": "object"}),
        ("output_schema", str),
        ("output_schema", BaseModel),
        ("output_schema", SampleInput),
        ("output_schema", SampleOutput(accepted=True)),
        ("output_schema", {"type": "object"}),
    ],
)
def test_invalid_schema_reference_is_rejected(
    read_fields: dict[str, object], field: str, schema: object
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ToolSpec(**(read_fields | {field: schema}))

    assert exc_info.value.errors()[0]["loc"] == (field,)


def test_tool_spec_is_frozen(read_fields: dict[str, object]) -> None:
    spec = ToolSpec(**read_fields)

    with pytest.raises(ValidationError) as exc_info:
        spec.requires_approval = True

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"
    assert spec.requires_approval is False


def test_schema_classes_validate_typed_data(read_fields: dict[str, object]) -> None:
    spec = ToolSpec(**read_fields)

    tool_input = spec.input_schema.model_validate({"label": "sample", "count": 1})
    tool_output = spec.output_schema.model_validate({"accepted": True})

    assert tool_input == SampleInput(label="sample", count=1)
    assert tool_output == SampleOutput(accepted=True)


@pytest.mark.parametrize(
    ("schema", "data", "field", "error_type"),
    [
        (SampleInput, {"label": "sample", "count": "1"}, "count", "int_type"),
        (SampleInput, {"label": "sample", "count": 1, "extra": None}, "extra", "extra_forbidden"),
        (SampleOutput, {"accepted": 1}, "accepted", "bool_type"),
        (SampleOutput, {"accepted": True, "extra": None}, "extra", "extra_forbidden"),
    ],
)
def test_input_and_output_reject_invalid_data(
    schema: type[ToolInput] | type[ToolOutput], data: dict[str, object], field: str, error_type: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        schema.model_validate(data)

    error = exc_info.value.errors()[0]
    assert error["loc"] == (field,)
    assert error["type"] == error_type


def test_input_and_output_are_frozen() -> None:
    tool_input = SampleInput(label="sample", count=1)
    tool_output = SampleOutput(accepted=True)

    with pytest.raises(ValidationError, match="frozen_instance"):
        tool_input.count = 2
    with pytest.raises(ValidationError, match="frozen_instance"):
        tool_output.accepted = False


def test_input_schema_generates_json_schema(read_fields: dict[str, object]) -> None:
    spec = ToolSpec(**read_fields)

    schema = spec.input_schema.model_json_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"label", "count"}
    assert schema["properties"]["label"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
