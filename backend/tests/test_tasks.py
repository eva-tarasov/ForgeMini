from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from fake_app import create_test_app as create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def workspace_id(client: TestClient) -> str:
    response = client.post("/workspaces", json={"name": "Test Workspace"})
    assert response.status_code == 201
    return response.json()["id"]


def task_payload(workspace_id: str) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "goal": "  Build API  ",
        "acceptance_criteria": ["  Returns 201  ", "Can be retrieved"],
    }


def test_create_and_get_task(client: TestClient, workspace_id: str) -> None:
    created_response = client.post("/tasks", json=task_payload(workspace_id))

    assert created_response.status_code == 201
    created = created_response.json()
    assert UUID(created["id"])
    assert created == {
        "id": created["id"],
        "workspace_id": workspace_id,
        "goal": "Build API",
        "acceptance_criteria": ["Returns 201", "Can be retrieved"],
    }

    fetched_response = client.get(f"/tasks/{created['id']}")
    assert fetched_response.status_code == 200
    assert fetched_response.json() == created


def test_missing_task_returns_404(client: TestClient) -> None:
    response = client.get(f"/tasks/{uuid4()}")

    assert response.status_code == 404


def test_invalid_task_id_returns_422(client: TestClient) -> None:
    response = client.get("/tasks/not-a-uuid")

    assert response.status_code == 422


def test_missing_workspace_returns_404(client: TestClient) -> None:
    response = client.post("/tasks", json=task_payload(str(uuid4())))

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "WORKSPACE_NOT_FOUND",
            "message": "Workspace not found",
            "details": [],
        }
    }


def test_invalid_workspace_id_returns_422(client: TestClient) -> None:
    response = client.post("/tasks", json=task_payload("not-a-uuid"))

    assert response.status_code == 422


@pytest.mark.parametrize("goal", ["", "   ", "x" * 501])
def test_invalid_goal_returns_422(
    client: TestClient, workspace_id: str, goal: str
) -> None:
    payload = task_payload(workspace_id)
    payload["goal"] = goal

    assert client.post("/tasks", json=payload).status_code == 422


@pytest.mark.parametrize(
    "criteria",
    [[], [""], ["   "], ["x" * 301], ["valid"] * 11],
)
def test_invalid_acceptance_criteria_returns_422(
    client: TestClient, workspace_id: str, criteria: list[str]
) -> None:
    payload = task_payload(workspace_id)
    payload["acceptance_criteria"] = criteria

    assert client.post("/tasks", json=payload).status_code == 422


def test_maximum_lengths_are_accepted(client: TestClient, workspace_id: str) -> None:
    payload = task_payload(workspace_id)
    payload["goal"] = "g" * 500
    payload["acceptance_criteria"] = ["c" * 300] * 10

    response = client.post("/tasks", json=payload)

    assert response.status_code == 201
    assert response.json()["goal"] == "g" * 500
    assert response.json()["acceptance_criteria"] == ["c" * 300] * 10


def test_non_keyed_create_preserves_http_control_separator(client: TestClient, workspace_id: str) -> None:
    goal = "\x1c" + "g" * 499
    criterion = "c" * 299 + "\x1f"
    response = client.post("/tasks", json={
        "workspace_id": workspace_id, "goal": f" {goal} ", "acceptance_criteria": [f" {criterion} "],
    })
    assert response.status_code == 201
    assert response.json()["goal"] == goal
    assert response.json()["acceptance_criteria"] == [criterion]


def test_multiple_tasks_can_share_workspace(client: TestClient, workspace_id: str) -> None:
    first = client.post("/tasks", json=task_payload(workspace_id))
    second = client.post("/tasks", json=task_payload(workspace_id))

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["workspace_id"] == second.json()["workspace_id"] == workspace_id


def test_idempotent_http_replay_and_conflicts(client: TestClient, workspace_id: str) -> None:
    headers = {"Idempotency-Key": str(uuid4())}
    payload = task_payload(workspace_id)
    first = client.post("/tasks", json=payload, headers=headers)
    replay = client.post("/tasks", json={**payload, "goal": "Build API",
                                        "acceptance_criteria": ["Returns 201", "Can be retrieved"]}, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert "idempotency_key" not in first.json()
    for changes in ({"goal": "Different"}, {"acceptance_criteria": ["Different"]},
                    {"acceptance_criteria": ["Can be retrieved", "Returns 201"]}):
        response = client.post("/tasks", json={**payload, **changes}, headers=headers)
        assert response.status_code == 409
        assert response.json() == {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT",
                                             "message": "Idempotency key already used with different task data",
                                             "details": []}}
    other_workspace = client.post("/workspaces", json={"name": "other"}).json()["id"]
    other = client.post("/tasks", json=task_payload(other_workspace), headers=headers)
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]


@pytest.mark.parametrize("change", [
    pytest.param({"goal": "\x1cBuild API"}, id="goal"),
    pytest.param({"acceptance_criteria": ["Returns 201\x1f", "Can be retrieved"]}, id="criterion"),
])
@pytest.mark.parametrize("control_first", [True, False], ids=["control-first", "plain-first"])
def test_http_replay_rejects_control_separator_difference(client, workspace_id, change, control_first):
    headers = {"Idempotency-Key": str(uuid4())}
    plain = {"workspace_id": workspace_id, "goal": "Build API", "acceptance_criteria": ["Returns 201", "Can be retrieved"]}
    control = {**plain, **change}
    first, second = (control, plain) if control_first else (plain, control)
    created = client.post("/tasks", json=first, headers=headers)
    assert created.status_code == 201
    assert created.json()["goal"] == first["goal"]
    assert created.json()["acceptance_criteria"] == first["acceptance_criteria"]

    padded = {
        **first,
        "goal": f"\u00a0{first['goal']}\u3000",
        "acceptance_criteria": [f"\u2003{criterion}\u202f" for criterion in first["acceptance_criteria"]],
    }
    replay = client.post("/tasks", json=padded, headers=headers)
    assert replay.status_code == 201 and replay.json() == created.json()

    conflict = client.post("/tasks", json=second, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT",
                                         "message": "Idempotency key already used with different task data",
                                         "details": []}}


def test_idempotency_header_validation_and_missing_workspace(client: TestClient, workspace_id: str) -> None:
    assert client.post("/tasks", json=task_payload(workspace_id),
                       headers={"Idempotency-Key": "not-a-uuid"}).status_code == 422
    response = client.post("/tasks", json=task_payload(str(uuid4())),
                           headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


def test_task_storage_is_isolated_by_fake_repository(client: TestClient, workspace_id: str) -> None:
    created = client.post("/tasks", json=task_payload(workspace_id)).json()

    with TestClient(create_app()) as other_client:
        assert other_client.get(f"/tasks/{created['id']}").status_code == 404
