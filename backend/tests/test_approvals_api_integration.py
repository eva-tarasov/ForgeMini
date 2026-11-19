"""Approval discovery on the existing dedicated PostgreSQL fixture database."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, event, insert, select
from test_agent_runs_start_cancel_integration import _client
from test_approval_integration import ORIGINAL, SENTINEL, TARGET, database, snapshot, waiting
from test_approvals_api import AUTH, TOKEN, assert_approval_http_logs, http_logs

from app.db.models import AgentRunModel, ApprovalModel, TaskModel, ToolCallModel, WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.main import create_app
from app.services.agent_runs import AgentRunService
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.pending_approvals import PendingApprovals


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_APPROVAL_TEST_DB") is None,
    reason="set FORGE_APPROVAL_TEST_DB and POSTGRES_DB to the dedicated fixture DB",
)


def _app(case, *, workspace_id=None, factory=None):
    assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
    return create_app(
        session_factory=case.factory if factory is None else factory,
        approval_access_binding=LocalApprovalAccessBinding(workspace_id or case.context.workspace_id, SecretStr(TOKEN)),
    )


async def _read(client, run_id, *, headers=AUTH):
    return await client.get(f"/agent-runs/{run_id}/approvals?status=pending", headers=headers)


async def _add_approvals(session, run_id, statuses):
    calls = [
        {"id": uuid4(), "agent_run_id": run_id, "tool_name": "apply_patch", "status": "proposed"}
        for _ in statuses
    ]
    approvals = [
        {"id": uuid4(), "tool_call_id": call["id"], "status": status,
         "decided_at": None if status == "pending" else datetime.now(UTC)}
        for call, status in zip(calls, statuses, strict=True)
    ]
    await session.execute(insert(ToolCallModel), calls)
    await session.execute(insert(ApprovalModel), approvals)
    return [approval["id"] for approval in approvals]


@asynccontextmanager
async def _other_run(case, *, same_workspace=False):
    workspace_id = case.context.workspace_id if same_workspace else uuid4()
    task_id, run_id = uuid4(), uuid4()
    async with case.factory.begin() as session:
        if not same_workspace:
            session.add(WorkspaceModel(id=workspace_id, name="synthetic approval discovery workspace"))
            await session.flush()
        session.add(TaskModel(id=task_id, workspace_id=workspace_id, goal="synthetic approval discovery",
                              acceptance_criteria=["isolated workspace"]))
        await session.flush()
        session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=1, status="waiting_approval"))
        await session.flush()
        [approval_id] = await _add_approvals(session, run_id, ["pending"])
    try:
        yield workspace_id, run_id, approval_id
    finally:
        async with case.factory.begin() as session:
            call_ids = select(ToolCallModel.id).where(ToolCallModel.agent_run_id == run_id)
            await session.execute(delete(ApprovalModel).where(ApprovalModel.tool_call_id.in_(call_ids)))
            await session.execute(delete(ToolCallModel).where(ToolCallModel.agent_run_id == run_id))
            await session.execute(delete(AgentRunModel).where(AgentRunModel.id == run_id))
            await session.execute(delete(TaskModel).where(TaskModel.id == task_id))
            if not same_workspace:
                await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))


def test_postgres_pending_discovery_projects_metadata_without_touching_waiter_or_state(database, monkeypatch, http_logs):
    async def check(case):
        def unexpected(*_args, **_kwargs):
            pytest.fail("SQL discovery must not use the live approval waiter or decision path")

        run_id = case.context.run_id
        async with _client(_app(case)) as client:
            empty = await _read(client, run_id)
            assert empty.status_code == 200 and empty.json() == {"items": []}

            async with waiting(case, monkeypatch) as (execution, operation):
                async with case.factory.begin() as session:
                    await _add_approvals(session, run_id, ["approved", "rejected"])
                before = await snapshot(case, fresh=True)
                async with _other_run(case, same_workspace=True) as (_, sibling, sibling_approval):
                    with monkeypatch.context() as blocked:
                        blocked.setattr(ApprovedToolExecutor, "decide", unexpected)
                        blocked.setattr(PendingApprovals, "request_for", unexpected)
                        response = await _read(client, run_id)
                        assert response.status_code == 200
                        [item] = response.json()["items"]
                        sibling_response = await _read(client, sibling)
                        assert sibling_response.status_code == 200
                        assert [entry["id"] for entry in sibling_response.json()["items"]] == [str(sibling_approval)]
                reference = operation.reference
                async with case.factory() as session:
                    requested = await session.scalar(
                        select(ApprovalModel.requested_at).where(ApprovalModel.id == reference.approval_id)
                    )
                assert item == {
                    "id": str(reference.approval_id), "run_id": str(run_id), "tool_name": "apply_patch",
                    "status": "pending", "requested_at": item["requested_at"],
                }
                assert datetime.fromisoformat(item["requested_at"]) == requested
                assert not execution.done() and case.coordinator.pending_request(case.context) is reference
                assert await snapshot(case, fresh=True) == before
                assert case.target.read_bytes() == ORIGINAL

                logged = assert_approval_http_logs(http_logs, 200, 200, 200)
                output = empty.text + response.text + sibling_response.text + logged
                for hidden in (SENTINEL, TARGET, str(case.root), reference.patch_sha256, reference.sources_sha256,
                               TOKEN, operation.patch, json.dumps(operation.patch)[1:-1],
                               str(reference.ticket.tool_call_id), str(case.context.workspace_id)):
                    assert hidden not in output

    database(check)


def test_postgres_capability_precedes_run_lookup_and_foreign_run_is_indistinguishable(database):
    async def check(case):
        async with _other_run(case) as (foreign_workspace, foreign_run, approval_id):
            run_id, unknown = case.context.run_id, uuid4()
            async with _client(_app(case)) as client:
                for headers in ({}, {"Authorization": "Bearer wrong-capability"}):
                    results = [await _read(client, identifier, headers=headers)
                               for identifier in (run_id, unknown, foreign_run)]
                    assert {response.status_code for response in results} == {403}
                    assert len({response.text for response in results}) == 1
                    assert results[0].json()["error"]["code"] == "APPROVAL_ACCESS_DENIED"

                foreign = await _read(client, foreign_run)
                absent = await _read(client, unknown)
                assert foreign.status_code == absent.status_code == 404
                assert foreign.json() == absent.json() == {"error": {
                    "code": "AGENT_RUN_NOT_FOUND", "message": "AgentRun not found", "details": [],
                }}
                assert str(approval_id) not in foreign.text

            async with _client(_app(case, workspace_id=foreign_workspace)) as other_client:
                response = await _read(other_client, foreign_run)
                assert response.status_code == 200
                assert [entry["id"] for entry in response.json()["items"]] == [str(approval_id)]

    database(check)


def test_postgres_discovery_reads_committed_snapshot_with_fixed_sql_budget(database):
    async def check(case):
        engine = create_engine(case.settings)
        selects = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def record(_connection, _cursor, statement, parameters, _context, _executemany):
            selects.append((statement, parameters))

        run_id = case.context.run_id
        try:
            async with _client(_app(case, factory=create_session_factory(engine))) as client:
                async with case.factory() as writer:
                    expected = sorted(await _add_approvals(writer, run_id, ["pending"] * 34))
                    uncommitted = await _read(client, run_id)
                    assert uncommitted.status_code == 200 and uncommitted.json() == {"items": []}
                    await writer.commit()

                # This run has no live waiter. Discovery returns persisted metadata only.
                assert case.coordinator.pending_request(case.context) is None
                committed = await _read(client, run_id)
                assert committed.status_code == 200
                assert [item["id"] for item in committed.json()["items"]] == [str(key) for key in expected[:32]]
        finally:
            await engine.dispose()

        assert len(selects) == 2
        for statement, parameters in selects:
            assert statement.lstrip().startswith("SELECT")
            assert "LEFT OUTER JOIN LATERAL" in statement and "LIMIT" in statement
            assert 32 in parameters and case.context.workspace_id in parameters
            assert "tasks.workspace_id" in statement and "tool_calls.agent_run_id" in statement
            assert "agent_events" not in statement and "FOR UPDATE" not in statement
        assert (await snapshot(case, fresh=True))[4] == []

    database(check)


def test_postgres_discovery_during_cancel_never_exposes_foreign_approval(database, monkeypatch):
    async def check(case):
        closing, release = asyncio.Event(), asyncio.Event()
        record = case.coordinator._gate._record_decision

        async def pause_after_staging(*args, **kwargs):
            await record(*args, **kwargs)
            closing.set()
            await asyncio.wait_for(release.wait(), timeout=5)

        monkeypatch.setattr(case.coordinator._gate, "_record_decision", pause_after_staging)
        run_id = case.context.run_id
        async with (
            _other_run(case) as (foreign_workspace, foreign_run, foreign_approval),
            _client(_app(case)) as client,
            _client(_app(case, workspace_id=foreign_workspace)) as outsider,
            waiting(case, monkeypatch) as (execution, operation),
        ):
            async with case.factory() as session:
                await AgentRunService(session).cancel_execution(run_id)
            execution.cancel()
            try:
                await asyncio.wait_for(closing.wait(), timeout=3)
                stale = await _read(client, run_id)
                assert stale.status_code == 200
                assert [item["id"] for item in stale.json()["items"]] == [str(operation.reference.approval_id)]
                assert str(foreign_approval) not in stale.text
                assert (await _read(client, foreign_run)).status_code == 404
                assert (await _read(outsider, run_id)).status_code == 404
            finally:
                release.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution, timeout=5)
            settled = await _read(client, run_id)
            assert settled.status_code == 200 and settled.json() == {"items": []}
            run, _, [(call_id, call_status)], [(approval_id, approval_status, _)], _ = await snapshot(case, fresh=True)
            assert (run, call_status, approval_status) == ("cancelled", "failed", "rejected")
            assert (call_id, approval_id) == (operation.reference.ticket.tool_call_id, operation.reference.approval_id)
            assert case.target.read_bytes() == ORIGINAL

    database(check)
