import json
import socket
import traceback
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import (
    MAX_EVENT_SEQUENCE,
    AgentRunResponse,
    HealthResponse,
    TaskCreate,
    TaskResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)
from forge_cli.errors import ForgeAPIError, ForgeResponseError, ForgeTransportError

WORKSPACE_ID = UUID("fa229b71-c6ad-4401-857f-b8204c3121e2")
TASK_ID = UUID("90cdd605-647e-4f27-8cbb-600eecb1578a")
RUN_ID = UUID("51f33760-eeb4-4f34-b56f-18456d00b84f")
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


@pytest.fixture
def run_body():
    return {
        "id": str(RUN_ID),
        "task_id": str(TASK_ID),
        "attempt_number": 1,
        "status": "queued",
        "created_at": "2026-11-21T09:30:00+03:00",
    }


def public_event(sequence, kind="other", details=None):
    return {
        "sequence": sequence, "created_at": "2026-11-21T09:30:00Z",
        "kind": kind, "details": {} if details is None else details,
    }


@pytest.fixture
def run_result_body():
    return {
        "run_id": str(RUN_ID),
        "run_status": "queued",
        "preliminary_status": None,
        "execution_failure_code": None,
        "verification_verdict": None,
        "usage": None,
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


def test_create_run_accepts_201_and_200_without_starting(run_body):
    requests = []
    statuses = iter((201, 200))

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/tasks/{TASK_ID}/runs"
        assert request.content == b""
        assert "content-type" not in request.headers
        return httpx.Response(next(statuses), json=run_body)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        initial = client.create_run(TASK_ID)
        assert len(requests) == 1
        repeated = client.create_run(TASK_ID)

    assert len(requests) == 2
    assert isinstance(initial, AgentRunResponse)
    assert repeated == initial
    assert (initial.id, initial.task_id, initial.attempt_number, initial.status) == (
        RUN_ID, TASK_ID, 1, "queued"
    )
    assert initial.created_at.utcoffset() == timedelta(hours=3)


def test_get_run_reads_metadata(run_body):
    run_body.update(status="waiting_approval", attempt_number=2)

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/agent-runs/{RUN_ID}"
        return httpx.Response(200, json=run_body)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = client.get_run(RUN_ID)

    assert result.model_dump(mode="json") == run_body


@pytest.mark.parametrize("http_status", [200, 201])
@pytest.mark.parametrize(
    "changes",
    [{"task_id": str(WORKSPACE_ID)}, {"attempt_number": 2}, {"status": "running"}],
    ids=["wrong-task", "later-attempt", "not-queued"],
)
def test_create_run_rejects_inconsistent_metadata(run_body, http_status, changes):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(http_status, json={**run_body, **changes, "note": SECRET})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.create_run(TASK_ID)

    error = caught.value
    assert error.status_code == http_status
    assert error.method == "POST"
    assert error.endpoint == "/tasks/{task_id}/runs"
    assert f"HTTP {http_status}" in str(error)
    assert SECRET not in str(error)
    assert str(TASK_ID) not in str(error)
    assert str(WORKSPACE_ID) not in str(error)
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", f"/tasks/{TASK_ID}/runs")
    ]


def test_get_run_rejects_mismatched_id(run_body):
    run_body["id"] = str(WORKSPACE_ID)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=run_body))

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.get_run(RUN_ID)

    error = caught.value
    assert error.status_code == 200
    assert error.method == "GET"
    assert error.endpoint == "/agent-runs/{run_id}"
    assert "HTTP 200" in str(error)
    assert str(RUN_ID) not in str(error)
    assert str(WORKSPACE_ID) not in str(error)


@pytest.mark.parametrize(
    "operation, status",
    [("create_run", 202), ("create_run", 204), ("create_run", 307), ("get_run", 201)],
)
def test_run_rejects_unexpected_http_status(run_body, operation, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=run_body, headers={"Location": "/unexpected"})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeAPIError) as caught:
            if operation == "create_run":
                client.create_run(TASK_ID)
            else:
                client.get_run(RUN_ID)

    assert caught.value.status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "invalid"},
        {"task_id": None},
        {"attempt_number": 0},
        {"attempt_number": True},
        {"attempt_number": "1"},
        {"status": "unknown"},
        {"created_at": "2026-11-21T09:30:00"},
        {"created_at": "123"},
        {"created_at": 123},
    ],
)
def test_agent_run_rejects_malformed_metadata(run_body, changes):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={**run_body, **changes})
    )
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError):
            client.get_run(RUN_ID)


def test_run_creation_validates_reused_metadata():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.create_run(TASK_ID)

    assert caught.value.status_code == 200


@pytest.mark.parametrize("action, status", [("start", 202), ("cancel", 200)])
def test_run_mutation_posts_once_without_following_location(run_body, action, status):
    if action == "start":
        body = {"run_id": str(RUN_ID), "accepted": True}
    else:
        body = {**run_body, "status": "cancelled"}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, json=body, headers={"Location": f"https://other.example.invalid/{SECRET}"}
        )

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = getattr(client, f"{action}_run")(RUN_ID)

    assert result.model_dump(mode="json") == body
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/agent-runs/{RUN_ID}/{action}"
    assert request.content == b""
    assert "content-type" not in request.headers


@pytest.mark.parametrize("accepted", [False, 0, 1, 1.0, "true", "1", None])
def test_start_run_requires_literal_true(accepted):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json={"run_id": str(RUN_ID), "accepted": accepted})
    )

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.start_run(RUN_ID)

    assert caught.value.status_code == 202
    assert caught.value.endpoint == "/agent-runs/{run_id}/start"


@pytest.mark.parametrize(
    "action, status, changes",
    [
        ("start", 202, {"run_id": str(TASK_ID)}),
        ("cancel", 200, {"id": str(TASK_ID)}),
        ("cancel", 200, {"status": "running"}),
    ],
    ids=["start-wrong-id", "cancel-wrong-id", "cancel-wrong-status"],
)
def test_run_mutation_rejects_inconsistent_metadata(run_body, action, status, changes):
    body = (
        {"run_id": str(RUN_ID), "accepted": True}
        if action == "start"
        else {**run_body, "status": "cancelled"}
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={**body, **changes, "note": SECRET})
    )

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            getattr(client, f"{action}_run")(RUN_ID)

    error = caught.value
    assert error.status_code == status
    assert error.method == "POST"
    assert error.endpoint == f"/agent-runs/{{run_id}}/{action}"
    assert f"HTTP {status}" in str(error)
    for private_value in (SECRET, BASE_URL, str(RUN_ID), str(TASK_ID)):
        assert private_value not in str(error)


@pytest.mark.parametrize(
    "action, status",
    [("start", 200), ("start", 201), ("start", 204), ("start", 307),
     ("cancel", 201), ("cancel", 202), ("cancel", 204), ("cancel", 307)],
)
def test_run_mutation_rejects_unexpected_http_status(action, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "/unexpected"})

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgeAPIError) as caught:
            getattr(client, f"{action}_run")(RUN_ID)

    assert caught.value.status_code == status
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", f"/agent-runs/{RUN_ID}/{action}")
    ]


def test_start_run_interruption_does_not_send_cancel():
    requests = []

    def handler(request):
        requests.append(request)
        raise KeyboardInterrupt

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(KeyboardInterrupt):
            client.start_run(RUN_ID)

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", f"/agent-runs/{RUN_ID}/start")
    ]


def test_get_run_events_decodes_every_public_kind_and_preserves_alias():
    kinds_and_details = [
        ("agent_run_status_changed", {"from": "queued", "to": "running"}),
        ("agent_result_recorded", {"preliminary_status": "ready_for_verification"}),
        ("agent_execution_failed", {"code": "provider_timeout"}),
        ("verification_recorded", {"verdict": "inconclusive"}),
        ("tool_call_proposed", {}),
        ("tool_call_started", {}),
        ("tool_call_finished", {}),
        ("approval_requested", {}),
        ("approval_decided", {}),
        ("agent_usage_recorded", {}),
        ("other", {}),
    ]
    page = {
        "items": [public_event(index * 2, kind, details)
                  for index, (kind, details) in enumerate(kinds_and_details, 1)],
        "next_after_sequence": 22, "has_more": False,
    }
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/agent-runs/{RUN_ID}/events"
        assert dict(request.url.params) == {"after_sequence": "0", "limit": "50"}
        return httpx.Response(200, json=page)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = client.get_run_events(RUN_ID)

    assert len(calls) == 1
    assert result.items[0].created_at.utcoffset() == timedelta(0)
    assert result.model_dump(mode="json") == page


@pytest.mark.parametrize(
    "after, limit, sequences, has_more",
    [
        (MAX_EVENT_SEQUENCE, 1, [], False),
        (0, 100, [MAX_EVENT_SEQUENCE], False),
        (10, 2, [12, 15], True),
        (10, 2, [12, 15], False),
        (10, 2, [], False),
    ],
)
def test_get_run_events_fetches_one_page(after, limit, sequences, has_more):
    page = {
        "items": [public_event(sequence) for sequence in sequences],
        "next_after_sequence": sequences[-1] if sequences else after,
        "has_more": has_more,
    }
    calls = []

    def handler(request):
        calls.append(request)
        assert dict(request.url.params) == {"after_sequence": str(after), "limit": str(limit)}
        return httpx.Response(200, json=page)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        response = client.get_run_events(RUN_ID, after_sequence=after, limit=limit)

    assert response.model_dump(mode="json") == page
    assert len(calls) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"after_sequence": -1}, {"after_sequence": MAX_EVENT_SEQUENCE + 1},
        {"after_sequence": True}, {"after_sequence": 1.0}, {"after_sequence": "1"},
        {"limit": 0}, {"limit": 101}, {"limit": False}, {"limit": 1.0}, {"limit": "1"},
    ],
)
def test_get_run_events_rejects_invalid_query_before_http(options):
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected HTTP request"))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ValueError, match="must be an integer"):
            client.get_run_events(RUN_ID, **options)


@pytest.mark.parametrize("operation", ["get_run_events", "get_run_result"])
def test_run_reads_require_uuid_before_http(operation):
    transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected HTTP request"))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(TypeError, match="run_id must be a UUID"):
            getattr(client, operation)("invalid")


@pytest.mark.parametrize(
    "sequences, next_cursor, has_more",
    [
        ([10], 10, False), ([9], 9, False), ([12, 12], 12, False), ([15, 12], 12, False),
        ([11, 12, 13], 13, False), ([12], 10, False), ([12], 13, False),
        ([], 11, False), ([], 10, True), ([12], 12, True),
        ([12, MAX_EVENT_SEQUENCE], MAX_EVENT_SEQUENCE, True),
    ],
)
def test_get_run_events_rejects_inconsistent_page(sequences, next_cursor, has_more):
    page = {
        "items": [public_event(sequence) for sequence in sequences],
        "next_after_sequence": next_cursor,
        "has_more": has_more,
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=page))

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.get_run_events(RUN_ID, after_sequence=10, limit=2)

    assert caught.value.status_code == 200
    assert caught.value.endpoint == "/agent-runs/{run_id}/events"


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "internal_event"},
        {"sequence": 0}, {"sequence": MAX_EVENT_SEQUENCE + 1}, {"sequence": True}, {"sequence": "1"},
        {"created_at": "2026-11-21T09:30:00"}, {"created_at": "123"},
        {"details": {"payload": SECRET}}, {"payload": SECRET},
        {"kind": "agent_run_status_changed", "details": {"previous": "queued", "to": "running"}},
        {"kind": "agent_run_status_changed", "details": {"from": "queued", "to": "invalid"}},
        {"kind": "agent_result_recorded", "details": {"preliminary_status": True}},
        {"kind": "agent_execution_failed", "details": {"code": "invalid"}},
        {"kind": "verification_recorded", "details": {"verdict": "pass", "private": SECRET}},
        {"kind": "approval_requested", "details": None},
    ],
)
def test_get_run_events_rejects_malformed_event(changes):
    page = {"items": [{**public_event(1), **changes}], "next_after_sequence": 1, "has_more": False}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=page))

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError, match="invalid response"):
            client.get_run_events(RUN_ID)


@pytest.mark.parametrize(
    "changes", [{"has_more": 0}, {"next_after_sequence": "0"}, {"payload": SECRET}],
)
def test_get_run_events_rejects_malformed_page_fields(changes):
    page = {"items": [], "next_after_sequence": 0, "has_more": False, **changes}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=page))
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError):
            client.get_run_events(RUN_ID)


@pytest.mark.parametrize(
    "status, preliminary, failure, verdict",
    [
        ("queued", None, None, None),
        ("running", None, None, None),
        ("waiting_approval", None, None, None),
        ("cancelled", None, None, None),
        ("verifying", "ready_for_verification", None, "inconclusive"),
        ("verifying", "ready_for_verification", None, None),
        ("succeeded", "ready_for_verification", None, "pass"),
        ("failed", "blocked", None, None),
        ("failed", None, "provider_timeout", None),
        ("failed", "ready_for_verification", None, "fail"),
    ],
)
def test_get_run_result_accepts_valid_public_snapshots(run_result_body, status, preliminary, failure, verdict):
    run_result_body.update(
        run_status=status, preliminary_status=preliminary,
        execution_failure_code=failure, verification_verdict=verdict,
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/agent-runs/{RUN_ID}/result"
        assert not request.url.query
        return httpx.Response(200, json=run_result_body)

    with ForgeAPIClient(BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = client.get_run_result(RUN_ID)

    assert result.model_dump(mode="json") == run_result_body
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": str(TASK_ID)}, {"run_status": "invalid"}, {"usage": {}}, {"payload": SECRET},
        {"run_status": "succeeded", "preliminary_status": "ready_for_verification"},
        {"run_status": "succeeded", "verification_verdict": "pass"},
        {"run_status": "verifying"},
        {"run_status": "verifying", "preliminary_status": "blocked"},
        {"run_status": "running", "preliminary_status": "ready_for_verification"},
        {"run_status": "cancelled", "execution_failure_code": "runner_timeout"},
        {"run_status": "failed", "preliminary_status": "blocked", "execution_failure_code": "runner_timeout"},
        {"run_status": "failed", "preliminary_status": "ready_for_verification", "verification_verdict": "pass"},
        {"run_status": "verifying", "preliminary_status": "ready_for_verification", "verification_verdict": "fail"},
        {"run_status": "cancelled", "verification_verdict": "inconclusive"},
    ],
)
def test_get_run_result_rejects_inconsistent_metadata(run_result_body, changes):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={**run_result_body, **changes})
    )
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError) as caught:
            client.get_run_result(RUN_ID)

    assert caught.value.status_code == 200
    assert caught.value.endpoint == "/agent-runs/{run_id}/result"
    assert str(TASK_ID) not in str(caught.value)


@pytest.mark.parametrize(
    "cost, usage_complete, cost_complete",
    [(None, False, False), ("0.0000", True, True), ("12.0500", False, False)],
)
def test_get_run_result_preserves_usage_values(run_result_body, cost, usage_complete, cost_complete):
    usage = {
        "known_total_tokens": 0, "usage_complete": usage_complete,
        "known_cost_usd": cost, "cost_complete": cost_complete,
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={**run_result_body, "usage": usage})
    )

    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        result = client.get_run_result(RUN_ID).model_dump(mode="json")

    assert result["usage"] == usage


@pytest.mark.parametrize(
    "changes",
    [
        {"known_total_tokens": -1}, {"known_total_tokens": True}, {"known_total_tokens": "0"},
        {"usage_complete": 1}, {"cost_complete": "false"}, {"known_cost_usd": 0},
        {"known_cost_usd": "-0.1"}, {"known_cost_usd": "1e-4"}, {"known_cost_usd": "1.2\n"},
        {"cost_complete": True}, {"known_cost_usd": None, "usage_complete": True, "cost_complete": True},
    ],
)
def test_get_run_result_rejects_invalid_usage(run_result_body, changes):
    usage = {
        "known_total_tokens": 0, "usage_complete": False,
        "known_cost_usd": "0.00", "cost_complete": False, **changes,
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={**run_result_body, "usage": usage})
    )
    with ForgeAPIClient(BASE_URL, transport=transport) as client:
        with pytest.raises(ForgeResponseError):
            client.get_run_result(RUN_ID)


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
