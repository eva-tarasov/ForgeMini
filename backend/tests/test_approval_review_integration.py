"""Cold approval reviews and workspace scoping on the dedicated PostgreSQL database."""

import os
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event
from test_agent_runs_start_cancel_integration import _client
from test_approval_integration import database, snapshot
from test_approval_review_api import REVIEW_AUTH, REVIEW_TOKEN, review_url
from test_approvals_api import AUTH, TOKEN
from test_approvals_api_integration import _add_approvals, _other_run

from app.db.session import create_engine, create_session_factory
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.local_approval_access import LocalApprovalAccessBinding


pytestmark = pytest.mark.skipif(os.getenv("FORGE_APPROVAL_TEST_DB") is None, reason="requires dedicated approval DB")


def test_postgres_review_is_scoped_and_cold_pending_is_not_reviewable(database):
    async def check(case):
        assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
        run_id, workspace_id = case.context.run_id, case.context.workspace_id
        async with case.factory.begin() as session:
            approvals = await _add_approvals(session, run_id, ["pending", "approved", "rejected"])
        before = await snapshot(case, fresh=True)
        engine = create_engine(case.settings)
        reads = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def record(_connection, _cursor, statement, parameters, _context, _many):
            reads.append((statement, parameters))

        def unexpected(*_args):
            pytest.fail("GET must not compose another execution")

        app = create_app(
            session_factory=create_session_factory(engine),
            execution_binding=RunExecutionBinding(workspace_id, unexpected),
            approval_access_binding=LocalApprovalAccessBinding(
                workspace_id, SecretStr(TOKEN), review_token=SecretStr(REVIEW_TOKEN),
            ),
        )
        try:
            async with (
                _other_run(case) as (foreign_workspace, foreign, foreign_approval),
                _other_run(case, same_workspace=True) as (_, sibling, sibling_approval),
                _client(app) as client,
            ):
                forbidden = await client.get(review_url(run_id, approvals[0]), headers=AUTH)
                assert forbidden.status_code == 403 and not reads
                for approval_id in approvals:
                    response = await client.get(review_url(run_id, approval_id), headers=REVIEW_AUTH)
                    assert response.status_code == 409
                    assert response.json() == {"error": {
                        "code": "APPROVAL_NOT_REVIEWABLE", "message": "Approval is no longer reviewable", "details": [],
                    }}

                denied_targets = [
                    (uuid4(), approvals[0]), (run_id, uuid4()), (foreign, foreign_approval),
                    (run_id, foreign_approval), (sibling, approvals[0]), (run_id, sibling_approval),
                ]
                for target in denied_targets:
                    response = await client.get(review_url(*target), headers=REVIEW_AUTH)
                    assert response.status_code == 404
                    assert response.json() == {"error": {
                        "code": "APPROVAL_NOT_FOUND", "message": "Approval not found", "details": [],
                    }}
                    assert str(foreign_workspace) not in response.text and str(foreign_approval) not in response.text

            targets = [(run_id, key) for key in approvals] + denied_targets
            for (statement, parameters), (target_run, target_approval) in zip(reads, targets, strict=True):
                assert statement.lstrip().startswith("SELECT") and "FOR UPDATE" not in statement
                assert all(join in statement for join in ("JOIN tool_calls", "JOIN agent_runs", "JOIN tasks"))
                assert "jsonb_agg" in statement and "LIMIT" in statement
                assert {workspace_id, target_run, target_approval, 2, "approval_requested"}.issubset(parameters)
        finally:
            await engine.dispose()
        assert await snapshot(case, fresh=True) == before

    database(check, status=AgentRunStatus.WAITING_APPROVAL)
