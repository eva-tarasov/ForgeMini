"""Opt-in PostgreSQL coverage for scoped approval details and live preview."""

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event, update
from test_agent_observability import agent_log_capture
from test_agent_runs_start_cancel_integration import _client, _command, _execution, _wait_for_cleanup
from test_approval_details_api import DETAIL_PATH, UNAVAILABLE, detail_url
from test_approval_integration import database as approval_database
from test_approval_integration import snapshot
from test_approval_review_api import REVIEW_AUTH, REVIEW_PATH, REVIEW_TOKEN, review_url
from test_approvals_api import AUTH, TOKEN, http_logs
from test_approvals_api_integration import _add_approvals, _other_run
from test_approved_repository_integration import WRITE_ID, _runner, _tool_reply, _wait_for_preview
from test_repository_agent_integration import API_KEY, FILE, _offline_model
from test_repository_agent_integration import database as repository_database
from test_verification_e2e_integration import _fresh_state

from app.core.logging import JsonFormatter
from app.db.models import AgentEventModel
from app.db.session import create_engine, create_session_factory
from app.main import create_app
from app.repositories.approvals import ApprovalRepository
from app.services import in_process_run_coordinator
from app.services.approval_contracts import patch_fingerprint
from app.services.approval_gate import ApprovalGate
from app.services.approval_preview import ApprovalPreviewTarget
from app.services.in_process_run_coordinator import RunExecutionBinding
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.pending_approvals import PendingApprovals
from app.services.tool_call_audit import ToolCallErrorCode
from app.tools.repository.patch import parse_patch


def _access(workspace_id, *, review_token=None):
    assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
    return LocalApprovalAccessBinding(workspace_id, SecretStr(TOKEN), review_token=review_token)


@pytest.mark.skipif(os.getenv("FORGE_APPROVAL_TEST_DB") is None, reason="requires the dedicated approval DB")
def test_postgres_approval_detail_scope_history_and_no_live_waiter(approval_database):
    async def check(case):
        run_id, workspace_id = case.context.run_id, case.context.workspace_id
        async with case.factory.begin() as session:
            ids = await _add_approvals(session, run_id, ["pending", "approved", "rejected"])
        before = await snapshot(case, fresh=True)
        engine = create_engine(case.settings)
        statements = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def record(_connection, _cursor, statement, parameters, _context, _many):
            statements.append((statement, parameters))

        app = create_app(session_factory=create_session_factory(engine), approval_access_binding=_access(workspace_id))
        try:
            async with (
                _other_run(case) as (foreign_workspace, foreign_run, foreign_approval),
                _other_run(case, same_workspace=True) as (_, sibling, sibling_approval),
                _client(app) as client,
            ):
                unauthorized = await client.get(detail_url(run_id, ids[0]), headers={"Authorization": "Bearer wrong"})
                assert unauthorized.status_code == 403 and not statements
                for key, status in zip(ids, ("pending", "approved", "rejected"), strict=True):
                    response = await client.get(detail_url(run_id, key), headers=AUTH)
                    assert response.status_code == 200
                    item = response.json()
                    assert (item["id"], item["run_id"], item["status"]) == (str(key), str(run_id), status)
                    assert (item["decided_at"] is None) == (status == "pending")
                    assert item["requested_at"] and item["preview"] == UNAVAILABLE
                    assert str(workspace_id) not in response.text

                denied_targets = [
                    (run_id, uuid4()), (uuid4(), ids[0]), (sibling, ids[0]),
                    (run_id, sibling_approval), (foreign_run, foreign_approval),
                ]
                for target in denied_targets:
                    response = await client.get(detail_url(*target), headers=AUTH)
                    assert response.status_code == 404
                    assert response.json() == {"error": {
                        "code": "APPROVAL_NOT_FOUND", "message": "Approval not found", "details": [],
                    }}
                    assert str(foreign_workspace) not in response.text and str(foreign_approval) not in response.text

            targets = [(run_id, key) for key in ids] + denied_targets
            for (statement, parameters), (expected_run, expected_approval) in zip(statements, targets, strict=True):
                assert statement.lstrip().startswith("SELECT") and "FOR UPDATE" not in statement
                assert "agent_events" not in statement
                assert all(part in statement for part in ("JOIN tool_calls", "JOIN agent_runs", "JOIN tasks"))
                assert parameters == (expected_approval, expected_run, workspace_id)
        finally:
            await engine.dispose()
        assert await snapshot(case, fresh=True) == before

    approval_database(check)


SECRET_OLD = "synthetic-private-original-line"
SECRET_CONTEXT = "synthetic-private-context-line"
SECRET_NEW = 'synthetic-private-credential "<script>synthetic-injection</script>"'
SECOND = "nested/other.txt"


@pytest.mark.skipif(os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None, reason="requires the dedicated repository DB")
@pytest.mark.parametrize("end", ["cancel", "timeout", "shutdown"])
def test_postgres_http_started_approval_preview_tracks_exact_live_operation(
    repository_database, monkeypatch, http_logs, agent_log_capture, end,
):
    async def check(case, _idle):
        root = case.binding.root
        (root / "nested").mkdir()
        originals = {
            FILE: f"{SECRET_OLD}\n{SECRET_CONTEXT}\nuntouched\nsecond hunk\n",
            SECOND: "second file\nkept line\n",
        }
        for path, original in originals.items():
            (root / path).write_text(original)
        patch = (
            f"--- a/{FILE}\n+++ b/{FILE}\n@@ -1,2 +1,3 @@\n-{SECRET_OLD}\n"
            f"+{SECRET_NEW}\n+another addition\n {SECRET_CONTEXT}\n"
            "@@ -4,1 +5,1 @@\n-second hunk\n+second hunk changed\n"
            f"--- a/{SECOND}\n+++ b/{SECOND}\n@@ -1,2 +1,1 @@\n-second file\n kept line\n"
        )
        parsed = parse_patch(patch)
        published, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        publish = PendingApprovals.publish

        def observe_publish(pending, operation):
            publish(pending, operation)
            published.set()

        async def unexpected_decide(*_args, **_kwargs):
            pytest.fail("Approval details GET invoked ApprovalGate.decide")

        monkeypatch.setattr(PendingApprovals, "publish", observe_publish)
        monkeypatch.setattr(ApprovalGate, "decide", unexpected_decide)

        async def reply(index, _payload):
            assert index == 0, "The wait must end without a write or further model round"
            return _tool_reply("apply_patch", WRITE_ID, patch=patch)

        async with _offline_model(reply) as (model, calls):
            runner = _runner(case, model, wait_seconds=8)
            app = create_app(
                session_factory=case.factory,
                execution_binding=RunExecutionBinding(case.binding.workspace_id, lambda _factory, _run: runner),
                approval_access_binding=_access(case.binding.workspace_id, review_token=SecretStr(REVIEW_TOKEN)),
            )
            async with _client(app) as client:
                assert (await _command(client, case.run_id, "start")).status_code == 202
                task = _execution(app)
                private = await _wait_for_preview(runner, task, published)
                ref = private.reference
                target = ApprovalPreviewTarget(
                    ref.approval_id, ref.ticket.run_id, ref.ticket.workspace_id,
                    ref.ticket.tool_call_id, ref.ticket.tool_name,
                )
                url = detail_url(case.run_id, ref.approval_id)
                before = await _fresh_state(case)
                live = await client.get(url, headers=AUTH)
                assert live.status_code == 200
                item = live.json()
                assert (item["id"], item["run_id"], item["status"], item["decided_at"]) == (
                    str(ref.approval_id), str(case.run_id), "pending", None,
                )
                assert item["preview"] == {
                    "state": "live_at_read", "content_included": False,
                    "files": [
                        {"path": FILE, "added_lines": 3, "removed_lines": 2, "hunk_count": 2},
                        {"path": SECOND, "added_lines": 0, "removed_lines": 1, "hunk_count": 1},
                    ],
                }
                assert [file["path"] for file in item["preview"]["files"]] == list(parsed.paths)
                assert [file["hunk_count"] for file in item["preview"]["files"]] == [len(f.hunks) for f in parsed.files]
                for field in ("approval_id", "run_id", "workspace_id", "tool_call_id", "tool_name"):
                    wrong = "read_file" if field == "tool_name" else uuid4()
                    assert runner.read_approval_preview(replace(target, **{field: wrong})) is None
                exact_url = review_url(case.run_id, ref.approval_id)
                authorized_review = await client.get(exact_url, headers=REVIEW_AUTH)
                assert authorized_review.status_code == 200
                assert authorized_review.headers["cache-control"] == "no-store"
                assert authorized_review.headers["content-type"].startswith("application/json")
                assert authorized_review.json() == {
                    "approval_id": str(ref.approval_id), "run_id": str(case.run_id),
                    "patch_sha256": ref.patch_sha256, "patch": patch,
                }
                assert patch_fingerprint(authorized_review.json()["patch"]) == ref.patch_sha256
                for private_value in (ref.sources_sha256, str(root), TOKEN, REVIEW_TOKEN):
                    assert private_value not in authorized_review.text
                read_only = await client.get(exact_url, headers=AUTH)
                assert read_only.status_code == 403
                after_read = await _fresh_state(case)
                assert before[0].status == after_read[0].status == "waiting_approval"
                assert before[1].status == after_read[1].status == "queued"
                assert [(x.id, x.status) for x in before[2]] == [(x.id, x.status) for x in after_read[2]]
                assert [(e.sequence, e.payload) for e in before[3]] == [(e.sequence, e.payload) for e in after_read[3]]
                assert [(x.id, x.status) for x in before[4]] == [(x.id, x.status) for x in after_read[4]]

                observed_errors = [read_only.text]
                if end == "cancel":
                    # Compare durable review evidence with the reference and exact bytes; uncommitted damage is invisible.
                    [request_event] = [e for e in before[3] if e.event_type == "approval_requested"]
                    audit_row = update(AgentEventModel).where(AgentEventModel.id == request_event.id)
                    bad_digest = "a" * 64
                    async with case.factory() as writer:
                        await writer.execute(audit_row.values(payload={**request_event.payload, "patch_sha256": bad_digest}))
                        uncommitted = await client.get(exact_url, headers=REVIEW_AUTH)
                        assert uncommitted.status_code == 200 and uncommitted.json() == authorized_review.json()
                        await writer.commit()
                    try:
                        corrupted = await client.get(exact_url, headers=REVIEW_AUTH)
                        assert corrupted.status_code == 500
                        assert corrupted.json()["error"]["code"] == "APPROVAL_REVIEW_FAILED"
                        assert bad_digest not in corrupted.text
                        observed_errors.append(corrupted.text)
                    finally:
                        async with case.factory.begin() as writer:
                            await writer.execute(audit_row.values(payload=request_event.payload))

                    # Both reads have the old pending SQL snapshot when cancel commits.
                    detail_loaded, review_loaded = asyncio.Event(), asyncio.Event()

                    def suspend_after_sql(read, loaded):
                        async def paused(*args, **kwargs):
                            row = await read(*args, **kwargs)
                            loaded.set()
                            await asyncio.wait_for(release.wait(), timeout=5)
                            return row
                        return paused

                    with monkeypatch.context() as changes:
                        changes.setattr(ApprovalRepository, "read_for_run",
                                        suspend_after_sql(ApprovalRepository.read_for_run, detail_loaded))
                        changes.setattr(ApprovalRepository, "read_for_review",
                                        suspend_after_sql(ApprovalRepository.read_for_review, review_loaded))
                        poll = asyncio.gather(client.get(url, headers=AUTH), client.get(exact_url, headers=REVIEW_AUTH))
                        try:
                            await asyncio.wait_for(asyncio.gather(detail_loaded.wait(), review_loaded.wait()), timeout=3)
                            cancelled = await _command(client, case.run_id, "cancel")
                            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                        finally:
                            release.set()
                        racing, racing_review = await asyncio.wait_for(poll, timeout=5)
                elif end == "timeout":
                    finish = ApprovalGate.finish

                    async def suspend_timeout(gate, operation, *, error, duration_ms):
                        assert error is ToolCallErrorCode.APPROVAL_TIMEOUT
                        closing.set()
                        await asyncio.wait_for(release.wait(), timeout=5)
                        await finish(gate, operation, error=error, duration_ms=duration_ms)

                    loop = asyncio.get_running_loop()
                    real_time = loop.time
                    with monkeypatch.context() as changes:
                        changes.setattr(ApprovalGate, "finish", suspend_timeout)
                        # Advance monotonic time beyond the real asyncio approval timeout, below the run deadline.
                        changes.setattr(loop, "time", lambda: real_time() + 9)
                        try:
                            await asyncio.wait_for(closing.wait(), timeout=3)
                            racing = await client.get(url, headers=AUTH)
                            racing_review = await client.get(exact_url, headers=REVIEW_AUTH)
                        finally:
                            release.set()
                        await _wait_for_cleanup(app, task)
                else:
                    cancel = in_process_run_coordinator.cancel_agent_run

                    async def suspend_shutdown(*args):
                        closing.set()
                        await asyncio.wait_for(release.wait(), timeout=5)
                        await cancel(*args)

                    with monkeypatch.context() as changes:
                        changes.setattr(in_process_run_coordinator, "cancel_agent_run", suspend_shutdown)
                        shutdown = asyncio.create_task(app.state.run_coordinator.close())
                        try:
                            await asyncio.wait_for(closing.wait(), timeout=3)
                            racing = await client.get(url, headers=AUTH)
                            racing_review = await client.get(exact_url, headers=REVIEW_AUTH)
                        finally:
                            release.set()
                        await asyncio.wait_for(shutdown, timeout=5)

                assert racing_review.status_code == 409
                assert racing_review.json()["error"]["code"] == "APPROVAL_NOT_REVIEWABLE"
                observed_errors.append(racing_review.text)
                assert racing.status_code == 200
                assert racing.json()["status"] == "pending" and racing.json()["preview"] == UNAVAILABLE
                await _wait_for_cleanup(app, task)
                settled = await client.get(url, headers=AUTH)
                assert settled.status_code == 200
                assert settled.json()["status"] == "rejected" and settled.json()["decided_at"]
                assert settled.json()["preview"] == UNAVAILABLE
                assert runner.read_approval_preview(target) is None
                settled_review = await client.get(exact_url, headers=REVIEW_AUTH)
                assert settled_review.status_code == 409
                assert settled_review.json()["error"]["code"] == "APPROVAL_NOT_REVIEWABLE"
                observed_errors.append(settled_review.text)

        assert len(calls) == 1
        assert TOKEN not in repr(calls) and REVIEW_TOKEN not in repr(calls)
        durable_run, _, [call], _, [approval] = await _fresh_state(case)
        assert (durable_run.status, call.status, approval.status) == (
            "failed" if end == "timeout" else "cancelled", "failed", "rejected",
        )
        assert {path: (root / path).read_text() for path in originals} == originals
        detail_logs = [record for record in http_logs if record.http_fields["route"] == DETAIL_PATH]
        assert [record.http_fields["status_code"] for record in detail_logs] == [200, 200, 200]
        logs = "".join(JsonFormatter().format(record) + repr(record.__dict__) for record in http_logs)
        agent_logs = agent_log_capture.getvalue()
        assert "agent_usage_observed" in agent_logs
        review_logs = [record.http_fields["status_code"] for record in http_logs
                       if record.http_fields["route"] == REVIEW_PATH]
        assert review_logs == ([200, 403, 200, 500, 409, 409] if end == "cancel" else [200, 403, 409, 409])
        output = live.text + racing.text + settled.text + "".join(observed_errors) + logs + agent_logs
        for value in (TOKEN, REVIEW_TOKEN, API_KEY, WRITE_ID, patch, SECRET_OLD, SECRET_CONTEXT, SECRET_NEW,
                      "synthetic-injection", str(root), str(ref.ticket.workspace_id), str(ref.ticket.tool_call_id),
                      ref.patch_sha256, ref.sources_sha256):
            assert value not in output

    repository_database(check)
