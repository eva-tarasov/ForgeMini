import json
import socket
import traceback
from uuid import UUID

import httpx
import pytest

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import HealthResponse, TaskCreate, TaskResponse, WorkspaceCreate, WorkspaceResponse
from forge_cli.errors import ForgeAPIError, ForgeResponseError, ForgeTransportError

WORKSPACE_ID = UUID("fa229b71-c6ad-4401-857f-b8204c3121e2")
TASK_ID = UUID("90cdd605-647e-4f27-8cbb-600eecb1578a")
KEY = UUID("7ec8f5ab-5459-43ba-8816-743f6595ae10")
BASE_URL = "https://forge.example.invalid"
SECRET = "sensitive-marker-DO-NOT-DISCLOSE"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests must use MockTransport")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


@pytest.fixture
def task_body():
    return {
        "id": str(TASK_ID), "workspace_id": str(WORKSPACE_ID),
        "goal": "goal", "acceptance_criteria": ["criterion"],
    }


def test_get_health_and_timeout_settings():
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == BASE_URL + "/health"
        assert request.extensions["timeout"] == {"connect": 2.0, "read": 5.0, "write": 5.0, "pool": 5.0}
        return httpx.Response(200, json={"status": "ok"})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        assert client.get_health() == HealthResponse(status="ok")


@pytest.mark.parametrize("items", [[], [{"id": str(WORKSPACE_ID), "name": "Forge"}]])
def test_list_workspaces(items):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/workspaces"
        return httpx.Response(200, json=items)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = client.list_workspaces()

    assert len(result) == len(items)
    if items:
        assert isinstance(result[0], WorkspaceResponse)
        assert result[0].id == WORKSPACE_ID
        assert result[0].name == "Forge"


def test_create_workspace_payload():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/workspaces"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"name": "Forge"}
        return httpx.Response(201, json={"id": str(WORKSPACE_ID), "name": "Forge"})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        assert client.create_workspace(WorkspaceCreate(name=" Forge ")).id == WORKSPACE_ID


@pytest.mark.parametrize("key", [KEY, None])
def test_create_task_body_and_idempotency_header(task_body, key):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/tasks"
        expected = {field: value for field, value in task_body.items() if field != "id"}
        assert json.loads(request.content) == expected
        assert request.headers.get("Idempotency-Key") == (str(key) if key else None)
        if key:
            assert request.headers.raw.count((b"Idempotency-Key", str(key).encode())) == 1
        return httpx.Response(201, json=task_body)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        payload = TaskCreate(workspace_id=WORKSPACE_ID, goal="goal", acceptance_criteria=["criterion"])
        assert client.create_task(payload, idempotency_key=key).id == TASK_ID

    assert len(calls) == 1


def test_get_task_uuid_in_url(task_body):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/tasks/{TASK_ID}"
        return httpx.Response(200, json=task_body)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        task = client.get_task(TASK_ID)

    assert isinstance(task, TaskResponse)
    assert task.id == TASK_ID
    assert task.workspace_id == WORKSPACE_ID


@pytest.mark.parametrize(
    "status,code",
    [(404, "TASK_NOT_FOUND"), (409, "IDEMPOTENCY_KEY_CONFLICT"), (422, "VALIDATION_ERROR")],
)
def test_backend_api_errors(status, code):
    def handler(request):
        return httpx.Response(status, json={"error": {
            "code": code, "message": SECRET, "details": [{"field": SECRET, "message": SECRET}],
        }})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeAPIError) as caught:
            if status == 404:
                client.get_task(TASK_ID)
            else:
                client.create_task(
                    TaskCreate(workspace_id=WORKSPACE_ID, goal="goal", acceptance_criteria=["criterion"]),
                    idempotency_key=KEY,
                )

    assert caught.value.status_code == status
    assert caught.value.expected_status == (200 if status == 404 else 201)
    assert caught.value.server_code == code
    assert SECRET not in str(caught.value)
    assert str(TASK_ID) not in str(caught.value)


@pytest.mark.parametrize("body", [
    b"<html>" + SECRET.encode(),
    b'{"error":{"code":"TASK_NOT_FOUND"}}',
    b'{"error":{"code":"TASK_NOT_FOUND","message":"bad","details":"bad"}}',
    b"[]",
])
def test_server_error_safe_fallback(body, capsys):
    def handler(request):
        return httpx.Response(500, content=body, headers={"X-Secret": SECRET})

    with ForgeAPIClient(BASE_URL + "/" + SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeAPIError) as caught:
            client.get_health()

    error = caught.value
    assert error.status_code == 500
    assert error.server_code is None
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert SECRET not in repr(vars(error))
    assert SECRET not in "".join(traceback.format_exception(error))
    assert BASE_URL not in str(error)
    assert capsys.readouterr() == ("", "")


def test_valid_envelope_preserves_unrecognized_code_without_printing_it():
    body = {"error": {"code": SECRET, "message": SECRET}}
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json=body))

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeAPIError) as caught:
            client.get_health()

    assert caught.value.server_code == SECRET
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "exception,reason",
    [
        (httpx.ConnectError, "connection"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.WriteTimeout, "timeout"),
        (httpx.PoolTimeout, "timeout"),
        (httpx.RemoteProtocolError, "transport_failure"),
    ],
)
def test_transport_failure_is_safe_and_not_retried(exception, reason, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception(SECRET + " " + str(request.url), request=request)

    with ForgeAPIClient(BASE_URL + "/" + SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeTransportError) as caught:
            client.get_health()

    error = caught.value
    assert error.reason == reason
    assert error.method == "GET"
    assert error.endpoint == "/health"
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert SECRET not in repr(vars(error))
    assert SECRET not in "".join(traceback.format_exception(error))
    assert len(calls) == 1
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("body", [
    b"not-json-" + SECRET.encode(),
    b"{}",
    b"[]",
    b'{"status":"' + SECRET.encode() + b'"}',
])
def test_malformed_success_response(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.get_health()

    assert caught.value.status_code == 200
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("field", ["id", "workspace_id"])
def test_invalid_uuid_response(task_body, field):
    task_body[field] = SECRET
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=task_body))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError):
            client.get_task(TASK_ID)


@pytest.mark.parametrize("body", [{}, {"id": "invalid", "name": "Forge"}])
def test_invalid_workspace_list_item(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[body]))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError):
            client.list_workspaces()


@pytest.mark.parametrize("status", [200, 202, 204, 301, 302, 307, 308])
def test_unexpected_status_and_redirects_are_not_success(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"id": str(WORKSPACE_ID), "name": "Forge"},
            headers={"Location": BASE_URL + "/redirect"},
        )

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeAPIError) as caught:
            client.create_workspace(WorkspaceCreate(name="Forge"))

    assert caught.value.status_code == status
    assert caught.value.expected_status == 201
    assert len(calls) == 1


def test_context_manager_owns_transport_and_closes_on_error():
    class TrackingTransport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    transport = TrackingTransport(lambda request: httpx.Response(500))
    client = ForgeAPIClient(BASE_URL, transport=transport)
    assert not client.is_closed

    with pytest.raises(ForgeAPIError):
        with client as entered:
            assert entered is client
            client.get_health()

    assert client.is_closed
    assert transport.closed
    with pytest.raises(RuntimeError, match="closed"):
        client.get_health()
    client.close()


def test_context_manager_closes_after_success():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        client.get_health()

    assert client.is_closed


def test_environment_is_disabled_and_base_path_is_preserved(monkeypatch):
    captured = {}
    original = httpx.Client

    def capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "Client", capture)
    monkeypatch.setenv("FORGE_BASE_URL", "https://other.example.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "invalid proxy")
    monkeypatch.setenv("SSL_CERT_FILE", "/missing/certificate")

    def handler(request):
        assert request.url.path == "/api/health"
        assert request.url.host == "forge.example.invalid"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok"})

    with ForgeAPIClient(BASE_URL + "/api", transport=httpx.MockTransport(handler)) as client:
        client.get_health()

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


@pytest.mark.parametrize(
    "base_url", [None, "", "https://user:password@forge.example.invalid", "ftp://forge.example.invalid"]
)
def test_requires_explicit_valid_base_url(base_url):
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))
    with pytest.raises(ValueError):
        ForgeAPIClient(base_url, transport=transport)


def test_uuid_arguments_reject_strings_before_transport():
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(TypeError, match="task_id must be a UUID"):
            client.get_task("../health")
        with pytest.raises(TypeError, match="idempotency_key must be a UUID"):
            client.create_task(
                TaskCreate(workspace_id=WORKSPACE_ID, goal="goal", acceptance_criteria=["criterion"]),
                idempotency_key="invalid",
            )
