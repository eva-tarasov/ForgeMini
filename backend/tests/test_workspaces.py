from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_list_workspaces_is_empty_initially(client: TestClient) -> None:
    response = client.get("/workspaces")

    assert response.status_code == 200
    assert response.json() == []


def test_create_workspace_and_list_it(client: TestClient) -> None:
    response = client.post("/workspaces", json={"name": "  My Workspace  "})

    assert response.status_code == 201
    created = response.json()
    assert UUID(created["id"])
    assert created["name"] == "My Workspace"

    listed = client.get("/workspaces")
    assert listed.status_code == 200
    assert listed.json() == [created]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_workspace_name_is_rejected(client: TestClient, name: str) -> None:
    response = client.post("/workspaces", json={"name": name})

    assert response.status_code == 422
    assert client.get("/workspaces").json() == []


def test_name_with_100_characters_is_accepted(client: TestClient) -> None:
    response = client.post("/workspaces", json={"name": "x" * 100})

    assert response.status_code == 201
    assert response.json()["name"] == "x" * 100


def test_workspaces_get_distinct_ids(client: TestClient) -> None:
    first = client.post("/workspaces", json={"name": "First"}).json()
    second = client.post("/workspaces", json={"name": "Second"}).json()

    assert first["id"] != second["id"]
    assert client.get("/workspaces").json() == [first, second]


def test_application_instances_have_separate_storage(client: TestClient) -> None:
    client.post("/workspaces", json={"name": "Only in first app"})

    with TestClient(create_app()) as other_client:
        assert other_client.get("/workspaces").json() == []
