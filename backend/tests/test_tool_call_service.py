import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.db.models import ToolCallModel
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus, ToolCallStatus
from app.permissions.context import PermissionContext
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tool_calls import ToolCallRepository
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode
from app.services.tool_calls import ToolCallService
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError


CONTEXT = PermissionContext(UUID(int=1), UUID(int=2), frozenset({Capability.REPOSITORY_READ}))
SPEC = ToolSpec(
    name="read_fixture", version=2, description="Synthetic audit test", input_schema=ToolInput,
    output_schema=ToolOutput, capability=Capability.REPOSITORY_READ, risk_level=ToolRiskLevel.LOW,
    timeout_ms=1000, side_effect=False, requires_approval=False,
)


@pytest.fixture
def audit_service(monkeypatch):
    trace, sessions, payloads = [], [], []
    state = SimpleNamespace(
        run=SimpleNamespace(task_id=UUID(int=3), status=AgentRunStatus.RUNNING.value),
        workspace_id=CONTEXT.workspace_id,
        call=None,
        append_error=None,
    )

    def new_session():
        def add(row):
            assert isinstance(row, ToolCallModel)
            trace.append("insert")
            state.call = row

        async def commit():
            trace.append("commit")

        async def rollback():
            trace.append("rollback")

        async def close():
            trace.append("close")

        session = SimpleNamespace(add=add, commit=commit, rollback=rollback, close=close)
        sessions.append(session)
        trace.append("open")
        return session

    async def lock_run(repo, run_id):
        assert repo.session is sessions[-1] and run_id == CONTEXT.run_id
        trace.append("parent-lock")
        return state.run

    async def get_task(repo, task_id):
        assert repo.session is sessions[-1] and task_id == state.run.task_id
        trace.append("workspace-check")
        return SimpleNamespace(workspace_id=state.workspace_id)

    async def lock_call(repo, run_id, tool_call_id):
        assert repo.session is sessions[-1]
        assert (run_id, tool_call_id) == (state.call.agent_run_id, state.call.id)
        trace.append("tool-lock")
        return state.call

    async def append(repo, run_id, *, event_type, payload):
        assert repo.session is sessions[-1] and run_id == CONTEXT.run_id
        assert state.call.status == payload["status"]
        trace.append(event_type)
        payloads.append(payload)
        if state.append_error is not None:
            raise state.append_error

    monkeypatch.setattr(AgentRunRepository, "get_for_update", lock_run)
    monkeypatch.setattr(TaskRepository, "get", get_task)
    monkeypatch.setattr(ToolCallRepository, "get_for_update", lock_call)
    monkeypatch.setattr(AgentEventRepository, "append", append)
    return ToolCallService(Mock(side_effect=new_session)), state, trace, sessions, payloads


def test_service_owns_separate_transactions_and_locks_parent_before_tool_call(audit_service):
    service, state, trace, sessions, payloads = audit_service

    async def check():
        ticket = await service.propose(CONTEXT, SPEC)
        assert (ticket.run_id, ticket.workspace_id, ticket.tool_name, ticket.tool_version) == (
            CONTEXT.run_id, CONTEXT.workspace_id, SPEC.name, SPEC.version,
        )
        assert ticket.tool_call_id == state.call.id
        await service.start(ticket)
        state.run.status = AgentRunStatus.CANCELLED.value
        await service.finish(ticket, ToolCallStatus.SUCCEEDED, duration_ms=4)
        assert state.run.status == AgentRunStatus.CANCELLED.value

    asyncio.run(check())
    assert len({id(session) for session in sessions}) == 3
    assert trace == [
        "open", "parent-lock", "workspace-check", "insert", "tool_call_proposed", "commit", "close",
        "open", "parent-lock", "workspace-check", "tool-lock", "tool_call_started", "commit", "close",
        "open", "parent-lock", "workspace-check", "tool-lock", "tool_call_finished", "commit", "close",
    ]
    assert [payload["status"] for payload in payloads] == ["proposed", "running", "succeeded"]
    assert payloads[-1]["duration_ms"] == 4


@pytest.mark.parametrize("case", ["missing", "queued", "cancelled", "workspace"])
def test_service_rejects_run_admission_without_inserting(audit_service, case):
    service, state, trace, _, _ = audit_service
    if case == "missing":
        state.run = None
    elif case == "workspace":
        state.workspace_id = UUID(int=5)
    else:
        state.run.status = case

    with pytest.raises((ToolCallAuditError, ToolExecutionError)) as caught:
        asyncio.run(service.propose(CONTEXT, SPEC))
    assert caught.value.code is (
        ToolErrorCode.PERMISSION_DENIED if case == "workspace" else ToolCallErrorCode.RUN_NOT_RUNNING
    )
    assert state.call is None
    assert trace[-2:] == ["rollback", "close"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_service_rolls_back_and_closes_on_audit_error_or_cancellation(audit_service, cancelled):
    service, state, trace, _, _ = audit_service
    original = asyncio.CancelledError("cancel request") if cancelled else RuntimeError("synthetic-db-secret")
    state.append_error = original

    with pytest.raises(asyncio.CancelledError if cancelled else ToolCallAuditError) as caught:
        asyncio.run(service.propose(CONTEXT, SPEC))
    if cancelled:
        assert caught.value is original
    else:
        assert str(caught.value) == "audit_persistence_failed"
        assert caught.value.__suppress_context__
    assert trace[-3:] == ["tool_call_proposed", "rollback", "close"]
