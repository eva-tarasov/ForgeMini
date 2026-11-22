from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from fake_app import create_test_app as create_app


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
    assert client.get("/workspaces").json() == sorted([first, second], key=lambda item: item["id"])


def test_separate_fake_repositories_are_isolated(client: TestClient) -> None:
    client.post("/workspaces", json={"name": "Only in first app"})

    with TestClient(create_app()) as other_client:
        assert other_client.get("/workspaces").json() == []


# ---- run configuration (ForgeMini): where the code is, how to test it, which image ----
def test_workspace_without_run_config_still_works_and_reports_nulls(client: TestClient) -> None:
    created = client.post("/workspaces", json={"name": "legacy"}).json()
    assert (created["source_path"], created["test_command"], created["image"]) == (None, None, None)


def test_workspace_run_config_is_stored_and_normalised(client: TestClient, tmp_path) -> None:
    body = {"name": "demo", "source_path": str(tmp_path), "test_command": "  python -m unittest ",
            "image": "python:3.12-slim"}
    created = client.post("/workspaces", json=body)
    assert created.status_code == 201
    data = created.json()
    assert data["source_path"] == str(tmp_path.resolve())
    assert data["test_command"] == "python -m unittest"          # stripped
    assert data["image"] == "python:3.12-slim"
    assert client.get("/workspaces").json() == [data]


@pytest.mark.parametrize("patch", [
    {"source_path": "relative/dir"},                 # must be absolute
    {"source_path": "/definitely/not/here/xyz"},      # must exist
    {"source_path": "bad\0path"},
    {"test_command": "   "},
    {"test_command": "x" * 1001},
    {"image": "bad image name"},                      # spaces are not allowed in image references
    {"image": "-leading-dash"},
])
def test_invalid_run_config_is_rejected(client: TestClient, patch: dict) -> None:
    response = client.post("/workspaces", json={"name": "demo", **patch})
    assert response.status_code == 422
    assert client.get("/workspaces").json() == []


def test_source_path_must_be_a_directory_not_a_file(client: TestClient, tmp_path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert client.post("/workspaces", json={"name": "demo", "source_path": str(f)}).status_code == 422
