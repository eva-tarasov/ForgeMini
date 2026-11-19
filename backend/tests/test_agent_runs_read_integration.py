"""Read-only projections from the dedicated PostgreSQL database."""

import asyncio
import os
import re
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete, event

from app.agent_runtime.structured_output import TaskAnalysis
from app.db.models import AgentEventModel, AgentRunModel
from app.domain.execution import ToolCallStatus
from app.permissions.context import PathScope
from app.repositories.agent_events import AgentEventRepository
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_runs import AgentRunService
from app.services.agent_usage import AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, TokenUsage, UsageStatus
from app.services.agent_usage_codec import AgentUsageRecord
from app.services.approval_contracts import ApprovalReference
from app.services.tool_call_audit import ToolCallAudit, encode_tool_call_event
from app.services.tool_calls import ToolCallTicket
from app.services.verification_contract import FileDigestCheck, VerificationBinding, VerificationPlan, fingerprint_criteria
from app.services.verification_evidence import encode_evidence
from app.services.verification_observer import TrustedFixtureAccess
from app.services.verification_service import VerificationService
from test_agent_runs_api_integration import _client
from test_repository_agent_integration import API_KEY, CONTENT, CRITERIA, FILE, _offline_model, _read_reply, _snapshot, database


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None,
    reason="set FORGE_REPOSITORY_AGENT_TEST_DB and POSTGRES_DB to the dedicated fixture DB",
)
PRIVATE = "synthetic-private /host/root file contents and patch"
READY = AgentFinalResult(
    summary=PRIVATE, status="ready_for_verification", relevant_components=(PRIVATE,), assumptions=(PRIVATE,),
    observations=(FixtureObservation(component="task_service", tool_call_id=PRIVATE, content=PRIVATE),),
    blocked_reason=None,
)


def _url(run_id, suffix):
    return f"/agent-runs/{run_id}/{suffix}"


async def _append(factory, run_id, *entries):
    async with factory.begin() as session:
        repository = AgentEventRepository(session)
        for kind, payload in entries:
            await repository.append(run_id, event_type=kind, payload=payload)


def _items(response):
    assert response.status_code == 200
    return response.json()["items"]


def _assert_private_absent(response, *additional):
    for value in (PRIVATE, "synthetic-private", "/host/root", *additional):
        assert value not in response.text


def _assert_safe_failure(response):
    assert response.status_code == 500
    assert response.json() == {"error": {
        "code": "AGENT_RUN_READ_FAILED", "message": "AgentRun data is unavailable", "details": [],
    }}
    _assert_private_absent(response)


def test_postgres_empty_page_preserves_cursor_and_missing_run_is_404(database):
    async def check(case, idle):
        async with _client(case.factory) as client:
            url = _url(case.run_id, "events")
            for cursor in (0, 2147483647):
                response = await client.get(url, params={"after_sequence": cursor})
                assert response.status_code == 200
                assert response.json() == {"items": [], "next_after_sequence": cursor, "has_more": False}
                idle()
            for suffix in ("events", "result"):
                response = await client.get(_url(uuid4(), suffix))
                assert response.status_code == 404 and response.json()["error"]["code"] == "AGENT_RUN_NOT_FOUND"
    database(check)


@pytest.mark.parametrize("limit", [1, 2])
def test_postgres_keyset_traverses_unknown_and_known_events_without_skips(database, limit):
    async def check(case, idle):
        other = uuid4()
        async with case.factory.begin() as session:
            session.add(AgentRunModel(id=other, task_id=case.task_id, attempt_number=2, status="queued"))
            await session.flush()
            await AgentEventRepository(session).append(other, event_type="foreign-private-kind", payload={"raw": PRIVATE})
        try:
            await _append(case.factory, case.run_id,
                          ("/host/root", {"raw": PRIVATE}),
                          ("agent_run_status_changed", {"from": "queued", "to": "running"}),
                          ("agent_result_recorded", {"schema_version": 1, "result_status": "blocked"}),
                          ("private-kind", {"raw": PRIVATE}))
            async with _client(case.factory) as client:
                cursor, seen = 0, []
                while True:
                    response = await client.get(_url(case.run_id, "events"), params={"after_sequence": cursor, "limit": limit})
                    page = _items(response)
                    assert page and all(set(item) == {"sequence", "kind", "created_at", "details"} for item in page)
                    cursor = response.json()["next_after_sequence"]
                    assert cursor == page[-1]["sequence"]
                    seen.extend((item["sequence"], item["kind"], item["details"]) for item in page)
                    _assert_private_absent(response, "private-kind")
                    idle()
                    if not response.json()["has_more"]:
                        break
                assert seen == [
                    (1, "other", {}), (2, "agent_run_status_changed", {"from": "queued", "to": "running"}),
                    (3, "agent_result_recorded", {"preliminary_status": "blocked"}), (4, "other", {}),
                ]
                response = await client.get(_url(case.run_id, "events"), params={"after_sequence": cursor})
                assert response.json() == {"items": [], "next_after_sequence": 4, "has_more": False}
        finally:
            async with case.factory.begin() as session:
                await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id == other))
                await session.execute(delete(AgentRunModel).where(AgentRunModel.id == other))
    database(check)


def test_postgres_poll_sees_new_events_only_after_commit_and_closes_sessions(database):
    async def check(case, idle):
        await _append(case.factory, case.run_id, ("known-only-to-backend", {}))
        async with _client(case.factory) as client:
            async with case.factory.begin() as writer:
                await AgentEventRepository(writer).append(case.run_id, event_type="future", payload={"raw": PRIVATE})
                await writer.flush()
                response = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
                assert response.json() == {"items": [], "next_after_sequence": 1, "has_more": False}
            idle()
            response = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
            [item] = _items(response)
            assert item["sequence"] == 2 and item["kind"] == "other" and item["details"] == {}
            _assert_private_absent(response)
            idle()
    database(check)


@pytest.mark.parametrize("status", ["queued", "running", "cancelled"])
def test_postgres_result_before_preliminary_contains_null_sections(database, status):
    async def check(case, idle):
        async with case.factory() as session:
            runs = AgentRunService(session)
            if status == "running":
                await runs.claim_for_execution(case.run_id)
            elif status == "cancelled":
                await runs.cancel_execution(case.run_id)
        async with _client(case.factory) as client:
            response = await client.get(_url(case.run_id, "result"))
            assert response.status_code == 200
            assert response.json() == {
                "run_id": str(case.run_id), "run_status": status, "preliminary_status": None,
                "execution_failure_code": None, "verification_verdict": None, "usage": None,
            }
        idle()
        run, task, _, _ = await _snapshot(case.factory, case)
        assert (run.status, task.status) == (status, "queued")
    database(check)


@pytest.mark.parametrize("outcome", ["ready", "blocked", "failure"])
def test_postgres_preliminary_and_execution_failure_are_separate_safe_projections(database, outcome):
    async def check(case, _idle):
        async with case.factory() as session:
            runs = AgentRunService(session)
            await runs.claim_for_execution(case.run_id)
            if outcome == "failure":
                await runs.fail_execution(case.run_id, AgentFailure(AgentFailureCode.PROVIDER_AUTH))
            else:
                preliminary = READY if outcome == "ready" else READY.model_copy(
                    update={"status": "blocked", "blocked_reason": PRIVATE}
                )
                await runs.complete_execution(case.run_id, preliminary)
        async with _client(case.factory) as client:
            result = await client.get(_url(case.run_id, "result"))
            assert result.status_code == 200
            expected_preliminary = {"ready": "ready_for_verification", "blocked": "blocked", "failure": None}[outcome]
            expected_failure = "provider_auth" if outcome == "failure" else None
            assert result.json() == {
                "run_id": str(case.run_id), "run_status": "verifying" if outcome == "ready" else "failed",
                "preliminary_status": expected_preliminary, "execution_failure_code": expected_failure,
                "verification_verdict": None, "usage": None,
            }
            events = await client.get(_url(case.run_id, "events"))
            details_kind = "agent_execution_failed" if expected_failure else "agent_result_recorded"
            [detail] = [item["details"] for item in _items(events) if item["kind"] == details_kind]
            assert detail == ({"code": expected_failure} if expected_failure else {"preliminary_status": expected_preliminary})
            _assert_private_absent(result)
            _assert_private_absent(events)
    database(check)


def _plan(case, expected=CONTENT.encode()):
    binding = VerificationBinding(case.run_id, case.task_id, case.binding.workspace_id, case.binding.baseline.revision)
    plan = VerificationPlan(
        uuid4(), binding, fingerprint_criteria(CRITERIA),
        (FileDigestCheck(uuid4(), 0, FILE, sha256(expected).hexdigest()),),
    )
    return plan, TrustedFixtureAccess(
        plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=binding,
        root=case.binding.root, read_scopes=(PathScope(FILE),), acceptance_criteria=CRITERIA,
    )


@pytest.mark.parametrize("expected", ["pass", "fail", "inconclusive", "cancelled-after-inconclusive"])
def test_postgres_result_uses_latest_decoded_verification_attempt(database, expected):
    async def check(case, _idle):
        async with case.factory() as session:
            runs = AgentRunService(session)
            await runs.claim_for_execution(case.run_id)
            await runs.complete_execution(case.run_id, READY)
        verifier = VerificationService(case.factory)
        plan, access = _plan(case, b"approved bytes differ" if expected == "fail" else CONTENT.encode())
        first = await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
        assert first.verdict.value == "inconclusive"
        if expected in ("pass", "fail"):
            await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=True)
        if expected == "cancelled-after-inconclusive":
            async with case.factory() as session:
                await AgentRunService(session).cancel_execution(case.run_id)
        async with _client(case.factory) as client:
            response = await client.get(_url(case.run_id, "result"))
            assert response.status_code == 200
            verdict = "inconclusive" if expected == "cancelled-after-inconclusive" else expected
            expected_status = {"pass": "succeeded", "fail": "failed", "inconclusive": "verifying",
                               "cancelled-after-inconclusive": "cancelled"}[expected]
            assert (response.json()["run_status"], response.json()["verification_verdict"]) == (expected_status, verdict)
            events = await client.get(_url(case.run_id, "events"))
            details = [item["details"] for item in _items(events) if item["kind"] == "verification_recorded"]
            assert details == ([{"verdict": "inconclusive"}, {"verdict": verdict}]
                               if expected in ("pass", "fail") else [{"verdict": "inconclusive"}])
            for reply in (response, events):
                _assert_private_absent(reply, FILE, plan.fingerprint, plan.checks[0].expected_sha256)
        _, task, _, _ = await _snapshot(case.factory, case)
        assert task.status == "queued"
    database(check)


def _usage_record(case, mode):
    tariff = ModelTariff("synthetic-private-provider", "synthetic-private-model", "v1",
                         Decimal("0.000001"), Decimal("0.000002"), Decimal("0.000001"))
    def reported(i, stage):
        return ModelCallUsage(i, stage, ModelCallOutcome.RETURNED, UsageStatus.REPORTED, TokenUsage(1, 1, 2))
    calls = [reported(1, ModelCallStage.RESEARCH)]
    if mode in ("partial", "unpriced"):
        calls.extend([
            ModelCallUsage(2, ModelCallStage.FINALIZATION, ModelCallOutcome.RETURNED, UsageStatus.INVALID, None),
            ModelCallUsage(3, ModelCallStage.RESEARCH, ModelCallOutcome.RETURNED, UsageStatus.MISSING, None),
        ])
    else:
        calls.append(reported(2, ModelCallStage.FINALIZATION))
    pricing = AgentPricing(None if mode == "unpriced" else tariff, tariff)
    return AgentUsageRecord(AgentUsageSnapshot(case.run_id, tuple(calls)), pricing)


@pytest.mark.parametrize("mode", ["complete", "partial", "unpriced"])
def test_postgres_usage_preserves_exact_decimal_and_incompleteness(database, mode):
    async def check(case, _idle):
        record = _usage_record(case, mode)
        async with case.factory() as session:
            service = AgentRunService(session)
            await service.claim_for_execution(case.run_id)
            await service.complete_execution(case.run_id, READY)
            await service.record_usage(record)
        async with _client(case.factory) as client:
            response = await client.get(_url(case.run_id, "result"))
            assert response.status_code == 200
            assert response.json()["usage"] == {
                "known_total_tokens": 4 if mode == "complete" else 2,
                "usage_complete": mode == "complete",
                "known_cost_usd": {"complete": "0.000000000006", "partial": "0.000000000003", "unpriced": None}[mode],
                "cost_complete": mode == "complete",
            }
            events = await client.get(_url(case.run_id, "events"))
            assert [item["details"] for item in _items(events) if item["kind"] == "agent_usage_recorded"] == [{}]
            _assert_private_absent(response)
            _assert_private_absent(events)
    database(check)


def test_postgres_tool_and_approval_events_are_validated_but_project_empty_details(database):
    async def check(case, _idle):
        call = uuid4()
        approval = uuid4()
        ticket = ToolCallTicket(call, case.run_id, case.binding.workspace_id, "apply_patch", 1)
        reference = ApprovalReference(approval, ticket, "a" * 64, "b" * 64)
        entries = (
            encode_tool_call_event(ToolCallAudit(call, "apply_patch", 1, ToolCallStatus.PROPOSED)),
            ("approval_requested", reference.requested_payload()),
            ("approval_decided", {"approval_id": str(approval), "tool_call_id": str(call), "decision": "approved",
                                  "schema_version": 1}),
            encode_tool_call_event(ToolCallAudit(call, "apply_patch", 1, ToolCallStatus.RUNNING)),
            encode_tool_call_event(ToolCallAudit(call, "apply_patch", 1, ToolCallStatus.SUCCEEDED, duration_ms=7)),
        )
        await _append(case.factory, case.run_id, *entries)
        async with _client(case.factory) as client:
            response = await client.get(_url(case.run_id, "events"))
            assert [(item["kind"], item["details"]) for item in _items(response)] == [(kind, {}) for kind, _ in entries]
            _assert_private_absent(response, str(call), str(approval), str(case.binding.workspace_id), "a" * 64,
                                   "b" * 64, "apply_patch")
    database(check)


@pytest.mark.parametrize("kind", [
    "agent_run_status_changed", "agent_result_recorded", "tool_call_finished", "approval_requested",
    "approval_decided", "agent_execution_failed", "agent_usage_recorded", "verification_recorded",
])
def test_postgres_malformed_known_event_fails_closed_without_echo(database, kind):
    async def check(case, _idle):
        await _append(case.factory, case.run_id, (kind, {"schema_version": True, "private": PRIVATE}))
        async with _client(case.factory) as client:
            _assert_safe_failure(await client.get(_url(case.run_id, "events")))
            if kind in ("agent_execution_failed", "agent_usage_recorded", "verification_recorded"):
                _assert_safe_failure(await client.get(_url(case.run_id, "result")))
    database(check)


@pytest.mark.parametrize("corruption", ["invalid-result", "succeeded-without-evidence", "duplicate-failure"])
def test_postgres_result_fails_closed_on_corrupt_or_contradictory_snapshot(database, corruption):
    async def check(case, _idle):
        async with case.factory.begin() as session:
            run = await session.get(AgentRunModel, case.run_id)
            if corruption == "invalid-result":
                run.final_result = {"schema_version": 1, "result": {"summary": PRIVATE}}
            elif corruption == "succeeded-without-evidence":
                run.status = "succeeded"
            else:
                run.status = "failed"
        if corruption == "duplicate-failure":
            row = ("agent_execution_failed", {"schema_version": 1, "code": "execution_failed"})
            await _append(case.factory, case.run_id, row, row)
        async with _client(case.factory) as client:
            _assert_safe_failure(await client.get(_url(case.run_id, "result")))
    database(check)


def test_postgres_offline_graph_public_reads_use_bounded_sql_and_do_not_leak(database):
    async def check(case, idle):
        analysis = TaskAnalysis(summary=PRIVATE, relevant_components=["repository"], assumptions=[], blocked_reason=None)

        async def reply(index, _payload):
            idle()
            if index == 0:
                return _read_reply()
            if index == 1:
                return "stop", {"content": "synthetic research"}
            assert index == 2
            return "stop", {"content": analysis.model_dump_json()}

        async with _offline_model(reply) as (model, calls):
            await case.service(model).execute_run(case.run_id)
            assert len(calls) == 3
        engine = case.factory.kw["bind"]
        queries = []

        def capture(_connection, _cursor, statement, parameters, _context, _executemany):
            queries.append((statement, parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            async with _client(case.factory) as client:
                result = await client.get(_url(case.run_id, "result"))
                assert result.status_code == 200 and result.json()["run_status"] == "verifying"
                assert len(queries) == 1 and "jsonb_agg" in queries[0][0]
                result_sql, result_parameters = queries[0]
                bound = re.search(r"FROM \(SELECT .*?ORDER BY agent_events\.sequence\s+LIMIT \$(\d+)", result_sql, re.S)
                assert bound is not None and result_parameters[int(bound.group(1)) - 1] == 513
                assert result.json()["usage"] == {
                    "known_total_tokens": 90, "usage_complete": True, "known_cost_usd": None, "cost_complete": False,
                }
                idle()
                queries.clear()
                events = await client.get(_url(case.run_id, "events"), params={"limit": 2})
                assert len(_items(events)) == 2 and events.json()["has_more"]
                assert len(queries) == 2
                page_sql, parameters = queries[-1]
                assert "ORDER BY agent_events.sequence" in page_sql and "agent_events.sequence >" in page_sql
                assert "LIMIT" in page_sql and parameters[-1] == 3
                assert all("FOR UPDATE" not in query and "OFFSET" not in query for query, _ in queries)
                for response in (result, events):
                    _assert_private_absent(response, CONTENT, FILE, API_KEY, str(case.binding.root))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        idle()
        run, task, _, _ = await _snapshot(case.factory, case)
        assert (run.status, task.status) == ("verifying", "queued")
    database(check)


def test_postgres_result_and_events_show_committed_preliminary_from_independent_sessions(database, monkeypatch):
    async def check(case, idle):
        async with case.factory() as session:
            await AgentRunService(session).claim_for_execution(case.run_id)
        staged, release = asyncio.Event(), asyncio.Event()
        async with case.factory() as writer, _client(case.factory) as client:
            commit = writer.commit

            async def hold_commit():
                await writer.flush()
                staged.set()
                await release.wait()
                await commit()

            monkeypatch.setattr(writer, "commit", hold_commit)
            writing = asyncio.create_task(AgentRunService(writer).complete_execution(case.run_id, READY))
            try:
                await staged.wait()
                pending = await client.get(_url(case.run_id, "result"))
                assert pending.status_code == 200
                assert (pending.json()["run_status"], pending.json()["preliminary_status"]) == ("running", None)
                pending_events = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
                assert pending_events.json() == {"items": [], "next_after_sequence": 1, "has_more": False}
                release.set()
                await writing
                result = await client.get(_url(case.run_id, "result"))
                assert result.status_code == 200
                assert (result.json()["run_status"], result.json()["preliminary_status"]) == (
                    "verifying", "ready_for_verification",
                )
                events = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
                assert [item["kind"] for item in _items(events)] == ["agent_result_recorded", "agent_run_status_changed"]
            finally:
                release.set()
                if not writing.done():
                    writing.cancel()
                await asyncio.gather(writing, return_exceptions=True)
        idle()
    database(check)


def test_postgres_result_rejects_duplicate_decoded_verification_attempt(database):
    async def check(case, _idle):
        async with case.factory() as session:
            service = AgentRunService(session)
            await service.claim_for_execution(case.run_id)
            await service.complete_execution(case.run_id, READY)
        plan, access = _plan(case)
        record = await VerificationService(case.factory).verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
        await _append(case.factory, case.run_id, ("verification_recorded", encode_evidence(record)))
        async with _client(case.factory) as client:
            _assert_safe_failure(await client.get(_url(case.run_id, "result")))
    database(check)


def test_postgres_result_history_limit_and_events_polling(database):
    async def check(case, idle):
        result_limit = 512
        async with case.factory() as session:
            service = AgentRunService(session)
            await service.claim_for_execution(case.run_id)
            await service.complete_execution(case.run_id, READY)
            await service.record_usage(_usage_record(case, "complete"))
        plan, access = _plan(case)
        verifier = VerificationService(case.factory)
        for _ in range(3):
            template = await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
            assert template.verdict.value == "inconclusive"

        _, _, _, baseline = await _snapshot(case.factory, case)
        # Count the three real attempts and one usage event against the requested result history limit.
        assert [item.event_type for item in baseline[-4:]] == ["agent_usage_recorded"] + ["verification_recorded"] * 3
        last_sequence = baseline[-1].sequence + result_limit - 4
        # Bulk-seed distinct valid attempts for the large fixture without changing the production append path.
        async with case.factory.begin() as session:
            session.add_all([
                AgentEventModel(
                    agent_run_id=case.run_id, sequence=sequence, event_type="verification_recorded",
                    payload=encode_evidence(replace(template, attempt_id=uuid4())),
                ) for sequence in range(baseline[-1].sequence + 1, last_sequence + 1)
            ])

        async with _client(case.factory) as client:
            at_limit = await client.get(_url(case.run_id, "result"))
            assert at_limit.status_code == 200
            assert at_limit.json()["verification_verdict"] == "inconclusive"
            assert at_limit.json()["usage"]["known_total_tokens"] == 4
            idle()

            await verifier.verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
            await _append(case.factory, case.run_id, ("private-kind", {"raw": PRIVATE}))
            _assert_safe_failure(await client.get(_url(case.run_id, "result")))
            idle()

            page = await client.get(_url(case.run_id, "events"), params={"after_sequence": last_sequence, "limit": 1})
            [item] = _items(page)
            assert (item["sequence"], item["kind"], item["details"]) == (
                last_sequence + 1, "verification_recorded", {"verdict": "inconclusive"},
            )
            assert page.json()["has_more"] and page.json()["next_after_sequence"] == last_sequence + 1
            next_page = await client.get(_url(case.run_id, "events"), params={
                "after_sequence": page.json()["next_after_sequence"], "limit": 1,
            })
            [item] = _items(next_page)
            assert (item["sequence"], item["kind"], item["details"]) == (last_sequence + 2, "other", {})
            assert not next_page.json()["has_more"]
            _assert_private_absent(next_page)
            idle()
    database(check)
