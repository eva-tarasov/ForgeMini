from uuid import UUID

import pytest
from pydantic import ValidationError

from forge_cli.dto import (
    ErrorResponse,
    HealthResponse,
    TaskCreate,
    TaskResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)

WORKSPACE_ID = UUID("fa229b71-c6ad-4401-857f-b8204c3121e2")
TASK_ID = UUID("90cdd605-647e-4f27-8cbb-600eecb1578a")


@pytest.mark.parametrize("name", ["a", " a ", "a" * 100])
def test_workspace_name_boundaries(name):
    assert WorkspaceCreate(name=name).name == name.strip()


@pytest.mark.parametrize("name", ["", " \n\t", "a" * 101, None, 123])
def test_workspace_name_rejects_invalid_input(name):
    with pytest.raises(ValidationError):
        WorkspaceCreate(name=name)


def test_task_request_normalizes_and_serializes_uuid():
    task = TaskCreate(
        workspace_id=str(WORKSPACE_ID), goal=" goal ", acceptance_criteria=[" criterion "]
    )

    assert task.workspace_id == WORKSPACE_ID
    assert isinstance(task.workspace_id, UUID)
    assert task.model_dump(mode="json") == {
        "workspace_id": str(WORKSPACE_ID), "goal": "goal", "acceptance_criteria": ["criterion"]
    }


def test_task_request_accepts_maximum_lengths():
    task = TaskCreate(
        workspace_id=WORKSPACE_ID, goal="g" * 500, acceptance_criteria=["c" * 300] * 10
    )

    assert len(task.goal) == 500
    assert len(task.acceptance_criteria) == 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", "invalid"),
        ("goal", ""),
        ("goal", " \t"),
        ("goal", "g" * 501),
        ("acceptance_criteria", []),
        ("acceptance_criteria", ["c"] * 11),
        ("acceptance_criteria", [""]),
        ("acceptance_criteria", [" \n"]),
        ("acceptance_criteria", ["c" * 301]),
        ("acceptance_criteria", [123]),
    ],
)
def test_task_request_constraints(field, value):
    payload = {"workspace_id": WORKSPACE_ID, "goal": "goal", "acceptance_criteria": ["criterion"]}
    payload[field] = value

    with pytest.raises(ValidationError):
        TaskCreate(**payload)


def test_response_mapping_preserves_uuid_and_wire_fields():
    workspace = WorkspaceResponse.model_validate({"id": str(WORKSPACE_ID), "name": "Forge"})
    task = TaskResponse.model_validate({
        "id": str(TASK_ID), "workspace_id": str(WORKSPACE_ID),
        "goal": "goal", "acceptance_criteria": ["criterion"],
    })

    assert isinstance(workspace.id, UUID)
    assert isinstance(task.id, UUID)
    assert isinstance(task.workspace_id, UUID)
    assert set(task.model_dump()) == {"id", "workspace_id", "goal", "acceptance_criteria"}


@pytest.mark.parametrize("status", ["OK", "failed", True, None])
def test_health_requires_ok(status):
    with pytest.raises(ValidationError):
        HealthResponse(status=status)


def test_error_envelope_details_are_optional_in_backend_schema():
    envelope = ErrorResponse.model_validate({"error": {"code": "HTTP_ERROR", "message": "Error"}})

    assert envelope.error.details == []


def test_error_envelope_validation_details():
    envelope = ErrorResponse.model_validate({"error": {
        "code": "VALIDATION_ERROR", "message": "Request validation failed",
        "details": [{"field": "body.goal", "message": "Field is required"}],
    }})

    assert envelope.error.details[0].field == "body.goal"
