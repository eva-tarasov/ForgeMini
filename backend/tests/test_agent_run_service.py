import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.execution import AgentRunStatus as Status
from app.domain.transitions import InvalidAgentRunTransition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.services.agent_runs import (
    AgentRunNotFoundError,
    AgentRunResultRequiredError, AgentRunService, StaleAgentRunStatusError,
    VerificationEvidenceRequiredError,
)


def make_service(status="queued"):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    service = AgentRunService(session)
    run = SimpleNamespace(id=uuid4(), status=status, final_result=None)
    service.runs.get_for_update = AsyncMock(return_value=run)
    service.events.add_status_changed = AsyncMock()
    return service, session, run


def test_success_commits_status_and_event_in_order():
    service, session, run = make_service()
    calls = []

    async def lock(_):
        calls.append("lock")
        return run

    async def add_event(id_, current, target):
        assert run.status == "running"
        assert (id_, current, target) == (run.id, Status.QUEUED, Status.RUNNING)
        calls.append("event")

    async def commit():
        calls.append("commit")

    service.runs.get_for_update.side_effect = lock
    service.events.add_status_changed.side_effect = add_event
    session.commit.side_effect = commit
    asyncio.run(service.transition_status(run.id, Status.QUEUED, Status.RUNNING))
    assert calls == ["lock", "event", "commit"]
    service.runs.get_for_update.assert_awaited_once_with(run.id)
    service.events.add_status_changed.assert_awaited_once_with(run.id, Status.QUEUED, Status.RUNNING)
    session.rollback.assert_not_awaited()
    assert service.runs.session is service.events.session is session


@pytest.mark.parametrize("actual,expected,target,error", [
    (None, Status.QUEUED, Status.RUNNING, AgentRunNotFoundError),
    ("running", Status.QUEUED, Status.RUNNING, StaleAgentRunStatusError),
    ("queued", Status.QUEUED, Status.SUCCEEDED, InvalidAgentRunTransition),
    ("failed", Status.FAILED, Status.RUNNING, InvalidAgentRunTransition),
    ("verifying", Status.VERIFYING, Status.SUCCEEDED, VerificationEvidenceRequiredError),
])
def test_rejected_transitions_rollback_without_event(actual, expected, target, error):
    service, session, run = make_service(actual)
    if actual is None:
        service.runs.get_for_update.return_value = None
    with pytest.raises(error):
        asyncio.run(service.transition_status(run.id, expected, target))
    service.events.add_status_changed.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
    if actual is not None:
        assert run.status == actual


def test_repeated_old_expected_does_not_append_event():
    service, session, run = make_service()
    asyncio.run(service.transition_status(run.id, Status.QUEUED, Status.RUNNING))
    with pytest.raises(StaleAgentRunStatusError) as exc:
        asyncio.run(service.transition_status(run.id, Status.QUEUED, Status.RUNNING))
    assert (exc.value.expected, exc.value.actual) == (Status.QUEUED, Status.RUNNING)
    assert run.status == "running"
    service.events.add_status_changed.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("failure", ["event", "commit"])
def test_write_failures_rollback(failure):
    service, session, run = make_service()
    operation = service.events.add_status_changed if failure == "event" else session.commit
    error = RuntimeError("failure")
    operation.side_effect = error
    with pytest.raises(RuntimeError, match="failure") as exc:
        asyncio.run(service.transition_status(run.id, Status.QUEUED, Status.RUNNING))
    assert exc.value is error
    service.events.add_status_changed.assert_awaited_once_with(run.id, Status.QUEUED, Status.RUNNING)
    session.rollback.assert_awaited_once()
    if failure == "event":
        session.commit.assert_not_awaited()
    else:
        session.commit.assert_awaited_once()


def test_cancelled_operation_rolls_back():
    service, session, run = make_service()
    service.events.add_status_changed.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.transition_status(run.id, Status.QUEUED, Status.RUNNING))
    service.events.add_status_changed.assert_awaited_once_with(run.id, Status.QUEUED, Status.RUNNING)
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("expected,target", [("queued", Status.RUNNING), (Status.QUEUED, "running")])
def test_invalid_status_types_rollback_before_lock(expected, target):
    service, session, run = make_service()
    with pytest.raises(TypeError):
        asyncio.run(service.transition_status(run.id, expected, target))
    service.runs.get_for_update.assert_not_awaited()
    service.events.add_status_changed.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_run_repository_locks_and_does_not_commit():
    session = SimpleNamespace(scalar=AsyncMock(return_value=None), commit=AsyncMock())
    assert asyncio.run(AgentRunRepository(session).get_for_update(uuid4())) is None
    statement = session.scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    assert statement.get_execution_options().get("populate_existing") is True
    session.commit.assert_not_awaited()


def test_event_repository_sequences_and_does_not_commit():
    run_id = uuid4()
    session = MagicMock(
        no_autoflush=MagicMock(),
        scalar=AsyncMock(side_effect=[run_id, 4]),
        flush=AsyncMock(),
        add=MagicMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    event = asyncio.run(AgentEventRepository(session).add_status_changed(run_id, Status.RUNNING, Status.VERIFYING))

    assert session.scalar.await_count == 2
    lock_statement, sequence_statement = (query.args[0] for query in session.scalar.await_args_list)
    lock = lock_statement.compile(dialect=postgresql.dialect())
    lock_sql = " ".join(str(lock).lower().split())
    assert lock_sql.startswith("select agent_runs.id from agent_runs")
    assert "where agent_runs.id =" in lock_sql
    assert "for update" in lock_sql
    assert list(lock.params.values()) == [run_id]

    sequence = sequence_statement.compile(dialect=postgresql.dialect())
    sequence_sql = " ".join(str(sequence).lower().split())
    assert "coalesce(max(agent_events.sequence)," in sequence_sql
    assert "+" in sequence_sql
    assert "where agent_events.agent_run_id =" in sequence_sql
    assert list(sequence.params.values()) == [0, 1, run_id]

    assert event.agent_run_id == run_id and event.sequence == 4
    assert event.event_type == "agent_run_status_changed"
    assert event.payload == {"from": "running", "to": "verifying"}
    assert session.mock_calls == [
        call.no_autoflush.__enter__(),
        call.scalar(lock_statement),
        call.no_autoflush.__exit__(None, None, None),
        call.flush(),
        call.scalar(sequence_statement),
        call.add(event),
    ]
    session.flush.assert_awaited_once_with()
    session.add.assert_called_once_with(event)
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


# ADR-003 (C4): RUNNING -> VERIFYING requires a stored result that ASKS for verification. Previously only the LangGraph
# runner tests reached these guards; the rule belongs to the kernel, so it is pinned here directly.
@pytest.mark.parametrize("final_result,reason", [
    (None, "final_result_required"),
    ({"schema_version": 1, "result": {
        "summary": "s", "status": "blocked", "relevant_components": [], "assumptions": [], "observations": [],
        "blocked_reason": "synthetic"}}, "result_not_ready_for_verification"),
])
def test_verifying_requires_a_ready_result_and_rolls_back(final_result, reason):
    service, session, run = make_service("running")
    run.final_result = final_result
    with pytest.raises(AgentRunResultRequiredError, match=reason):
        asyncio.run(service.transition_status(run.id, Status.RUNNING, Status.VERIFYING))
    service.events.add_status_changed.assert_not_awaited()
    session.rollback.assert_awaited_once()
    assert run.status == "running"
