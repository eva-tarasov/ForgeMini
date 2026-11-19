"""PostgreSQL integration for scoped persisted evidence reads."""

import asyncio
import os
import re
from dataclasses import replace
from hashlib import sha256
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event, update
from test_agent_observability import agent_log_capture
from test_agent_runs_read_integration import READY
from test_agent_runs_start_cancel_integration import _client
from test_approval_review_api import REVIEW_TOKEN
from test_approvals_api import AUTH, TOKEN, http_logs
from test_repository_agent_integration import API_KEY, CONTENT, FILE, _snapshot, database
from test_verification_e2e_integration import _execute_read, _issued_plan, fixture_run
from test_verification_evidence_api import DETAIL_PATH, LIST_PATH, evidence_url

from app.core.logging import JsonFormatter
from app.db.models import AgentEventModel, AgentRunModel
from app.main import create_app
from app.repositories.agent_events import AgentEventRepository
from app.services import verification_observer, verification_service
from app.services.agent_runs import AgentRunService
from app.services.local_approval_access import LocalApprovalAccessBinding
from app.services.verification_contract import (
    FileDigestCheck, FileDigestObservation, ObservationIssue, VerificationObservationSet, fingerprint_criteria,
)
from app.services.verification_evidence import VERIFICATION_RECORDED, encode_evidence
from app.services.verification_service import VerificationService
from app.tools.repository.filesystem import SafeRepositoryReader


pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_REPOSITORY_AGENT_TEST_DB") is None, reason="requires the dedicated repository DB",
)
NOT_FOUND = {"error": {"code": "VERIFICATION_EVIDENCE_NOT_FOUND", "message": "Verification evidence not found", "details": []}}
FAILED = {"error": {"code": "VERIFICATION_EVIDENCE_READ_FAILED", "message": "Verification evidence read failed", "details": []}}


def _app(case, *, workspace_id=None):
    assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
    return create_app(session_factory=case.factory, approval_access_binding=LocalApprovalAccessBinding(
        workspace_id or case.binding.workspace_id, SecretStr(TOKEN), review_token=SecretStr(REVIEW_TOKEN),
    ))


async def _read(client, run_id, attempt_id=None, **params):
    return await client.get(evidence_url(run_id, attempt_id), headers=AUTH, params=params)


def _data(response):
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def _preliminary(case):
    async with case.factory() as session:
        service = AgentRunService(session)
        await service.claim_for_execution(case.run_id)
        await service.complete_execution(case.run_id, READY)


async def _inconclusive(case):
    await _preliminary(case)
    plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
    return await VerificationService(case.factory).verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)


async def _append_evidence(case, *records):
    async with case.factory.begin() as session:
        repository = AgentEventRepository(session)
        for record in records:
            await repository.append(case.run_id, event_type=VERIFICATION_RECORDED, payload=record)


def _persisted_signature(state):
    run, task, calls, events = state
    return (
        run.status, run.final_result, task.status, [(call.id, call.status) for call in calls],
        [(record.id, record.sequence, record.event_type, record.payload) for record in events],
    )


def test_postgres_evidence_scope_and_empty_run(fixture_run):
    async def check(case, _idle):
        async with _client(_app(case)) as client:
            empty = await _read(client, case.run_id, after_sequence=14)
            assert _data(empty) == {
                "run_id": str(case.run_id), "items": [], "next_after_sequence": 14, "has_more": False,
            }
            record = await _inconclusive(case)
            assert _data(await _read(client, case.run_id, record.attempt_id))["attempt_id"] == str(record.attempt_id)
            missing_run = await _read(client, uuid4())
            missing_attempt = await _read(client, case.run_id, uuid4())
            wrong_pair = await _read(client, uuid4(), record.attempt_id)
        async with _client(_app(case, workspace_id=uuid4())) as outsider:
            foreign_list = await _read(outsider, case.run_id)
            foreign_attempt = await _read(outsider, case.run_id, record.attempt_id)
        for response in (missing_run, missing_attempt, wrong_pair, foreign_list, foreign_attempt):
            assert response.status_code == 404 and response.json() == NOT_FOUND

    fixture_run(check)


@pytest.mark.parametrize("terminal", ["pass", "fail", "cancelled"])
def test_postgres_offline_graph_evidence_paginates_real_attempts(
    fixture_run, monkeypatch, http_logs, agent_log_capture, terminal,
):
    async def check(case, idle):
        await _execute_read(case, idle)
        expected = b"synthetic mismatch in trusted plan" if terminal == "fail" else CONTENT.encode()
        plan, access = _issued_plan(case, [(FILE, expected)])
        verify = VerificationService(case.factory).verify
        attempts = [
            await verify(plan, access, attempt_id=uuid4(), isolated_fixture=False)
            for _ in range(2)
        ]
        if terminal == "cancelled":
            async with case.factory() as session:
                await AgentRunService(session).cancel_execution(case.run_id)
            expected_status = "cancelled"
        else:
            attempts.append(await verify(plan, access, attempt_id=uuid4(), isolated_fixture=True))
            expected_status = "succeeded" if terminal == "pass" else "failed"
        before = await _snapshot(case.factory, case)
        assert before[0].status == expected_status
        sequences = [record.sequence for record in before[3] if record.event_type == VERIFICATION_RECORDED]
        statements = []
        engine = case.factory.kw["bind"]

        def capture(_connection, _cursor, statement, parameters, _context, _many):
            statements.append((statement, parameters))

        assert "agent_usage_observed" in agent_log_capture.getvalue()
        results = []
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            with monkeypatch.context() as hooks:
                no_verify = AsyncMock(side_effect=AssertionError("GET cannot verify"))
                no_observe = AsyncMock(side_effect=AssertionError("GET cannot observe"))
                no_read = Mock(side_effect=AssertionError("GET cannot read fixture files"))
                hooks.setattr(VerificationService, "verify", no_verify)
                hooks.setattr(verification_service, "observe", no_observe)
                hooks.setattr(verification_observer, "observe", no_observe)
                hooks.setattr(SafeRepositoryReader, "read_text", no_read)
                async with _client(_app(case)) as client:
                    cursor = 0
                    for index, (sequence, evidence) in enumerate(zip(sequences, attempts, strict=True)):
                        response = await _read(client, case.run_id, after_sequence=cursor, limit=1)
                        summary = {
                            "attempt_id": str(evidence.attempt_id), "sequence": sequence,
                            "verdict": "inconclusive" if index < 2 else terminal,
                            "file_verdict": "fail" if terminal == "fail" else "pass",
                            "audit_complete": True, "isolated_fixture": index >= 2, "check_count": 1,
                        }
                        assert _data(response) == {
                            "run_id": str(case.run_id), "items": [summary], "next_after_sequence": sequence,
                            "has_more": index < len(attempts) - 1,
                        }
                        detail = await _read(client, case.run_id, evidence.attempt_id)
                        assert _data(detail) == {**summary, "checks": [{
                            "check_id": str(plan.checks[0].check_id), "criterion_index": 0, "path": FILE,
                            "outcome": "mismatch" if terminal == "fail" else "match",
                        }]}
                        assert FILE not in response.text
                        results.extend((response, detail))
                        cursor = sequence
                    empty = await _read(client, case.run_id, after_sequence=cursor)
                    assert _data(empty) == {"run_id": str(case.run_id), "items": [],
                                            "next_after_sequence": cursor, "has_more": False}
                no_verify.assert_not_awaited()
                no_observe.assert_not_awaited()
                no_read.assert_not_called()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)

        assert len(statements) == 2 * len(attempts) + 1
        for statement, parameters in statements:
            assert statement.lstrip().startswith("SELECT")
            assert "jsonb_agg" in statement and "JOIN tasks" in statement and "tasks.workspace_id =" in statement
            bound = re.search(r"ORDER BY agent_events\.sequence\s+LIMIT \$(\d+)", statement, re.S)
            assert bound and parameters[int(bound.group(1)) - 1] == 513
            assert case.binding.workspace_id in parameters and "FOR UPDATE" not in statement and "OFFSET" not in statement
        assert _persisted_signature(await _snapshot(case.factory, case)) == _persisted_signature(before)
        logs = [entry for entry in http_logs if entry.http_fields["route"] in {LIST_PATH, DETAIL_PATH}]
        assert len(logs) == len(statements)
        logged = "".join(JsonFormatter().format(entry) + repr(entry.__dict__) for entry in logs)
        assert FILE not in logged
        output = logged + agent_log_capture.getvalue() + "".join(response.text for response in results)
        for value in (TOKEN, REVIEW_TOKEN, API_KEY, CONTENT, str(case.binding.root), plan.fingerprint,
                      plan.binding.baseline_revision, *plan.criterion_digests, *access.acceptance_criteria,
                      attempts[0].result_sha256, attempts[0].audit_sha256, attempts[0].access_sha256,
                      plan.checks[0].expected_sha256, attempts[0].observations.observations[0].observed_sha256):
            assert value not in output

    fixture_run(check)


def test_postgres_evidence_observes_terminal_attempt_only_after_commit(fixture_run, monkeypatch):
    async def check(case, _idle):
        earlier = await _inconclusive(case)
        plan, access = _issued_plan(case, [(FILE, CONTENT.encode())])
        terminal_id = uuid4()
        staged, release = asyncio.Event(), asyncio.Event()
        append = AgentEventRepository.append

        async def hold(repository, run_id, *, event_type, payload):
            result = await append(repository, run_id, event_type=event_type, payload=payload)
            if event_type == "agent_run_status_changed" and payload == {"from": "verifying", "to": "succeeded"}:
                await repository.session.flush()
                staged.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            return result

        monkeypatch.setattr(AgentEventRepository, "append", hold)
        async with _client(_app(case)) as client:
            write = asyncio.create_task(VerificationService(case.factory).verify(
                plan, access, attempt_id=terminal_id, isolated_fixture=True,
            ))
            try:
                await asyncio.wait_for(staged.wait(), timeout=3)
                listing = _data(await _read(client, case.run_id, limit=1))
                assert [item["attempt_id"] for item in listing["items"]] == [str(earlier.attempt_id)]
                assert not listing["has_more"]
                hidden = await _read(client, case.run_id, terminal_id)
                assert hidden.status_code == 404 and hidden.json() == NOT_FOUND
            finally:
                release.set()
                evidence = await asyncio.wait_for(write, timeout=5)
            assert evidence.verdict.value == "pass"
            next_page = _data(await _read(client, case.run_id, after_sequence=listing["next_after_sequence"]))
            [item] = next_page["items"]
            assert (item["attempt_id"], item["verdict"]) == (str(terminal_id), "pass")
            assert _data(await _read(client, case.run_id, terminal_id))["checks"][0]["outcome"] == "match"

    fixture_run(check)


def test_postgres_evidence_detail_distinguishes_all_observation_outcomes(fixture_run):
    async def check(case, _idle):
        template = await _inconclusive(case)
        expected = sha256(b"synthetic expected").hexdigest()
        different = sha256(b"synthetic actual").hexdigest()
        checks = tuple(FileDigestCheck(uuid4(), index, path, expected) for index, path in [
            (0, "matched.txt"), (0, "mismatch.txt"), (0, "read.txt"), (0, "unknown.txt"),
            (0, "duplicated.txt"), (0, "absent.txt"), (0, "nested/shared.txt"), (1, "nested/shared.txt"),
        ])
        plan = replace(template.plan, checks=checks, criterion_digests=fingerprint_criteria(("first", "second")))
        a, b, c, d, e, _, g, h = [check.check_id for check in checks]
        observations = VerificationObservationSet(plan.plan_id, plan.binding, plan.fingerprint, (
            FileDigestObservation(a, expected), FileDigestObservation(b, different),
            FileDigestObservation(c, issue=ObservationIssue.READ_ERROR),
            FileDigestObservation(d, issue=ObservationIssue.UNKNOWN_EFFECT),
            FileDigestObservation(e, expected), FileDigestObservation(e, expected),
            FileDigestObservation(g, expected), FileDigestObservation(h, different),
        ))
        evidence = replace(template, attempt_id=uuid4(), plan=plan, observations=observations, isolated_fixture=True)
        await _append_evidence(case, encode_evidence(evidence))
        async with _client(_app(case)) as client:
            response = await _read(client, case.run_id, evidence.attempt_id)
            detail = _data(response)
        assert (detail["file_verdict"], detail["verdict"], detail["check_count"]) == ("inconclusive", "inconclusive", 8)
        assert [(item["check_id"], item["criterion_index"], item["path"], item["outcome"]) for item in detail["checks"]] == [
            (str(check.check_id), check.criterion_index, check.path, outcome)
            for check, outcome in zip(checks, ("match", "mismatch", "read_error", "unknown_effect",
                                               "ambiguous", "missing", "ambiguous", "ambiguous"), strict=True)
        ]
        assert expected not in response.text and different not in response.text

    fixture_run(check)


@pytest.mark.parametrize("damage", [
    "schema", "forged-verdict", "wrong-run", "wrong-task", "wrong-workspace", "result-digest",
    "malformed-observation", "duplicate-attempt", "inconsistent-terminal", "attempt-after-terminal",
])
def test_postgres_evidence_rejects_corrupt_history_before_selecting_page(fixture_run, damage):
    async def check(case, _idle):
        evidence = await _inconclusive(case)
        run, _, _, events = await _snapshot(case.factory, case)
        [original] = [row for row in events if row.event_type == VERIFICATION_RECORDED]
        value = encode_evidence(evidence)
        if damage == "duplicate-attempt":
            await _append_evidence(case, value)
        elif damage == "attempt-after-terminal":
            async with case.factory.begin() as session:
                stmt = update(AgentEventModel).where(AgentEventModel.id == original.id)
                await session.execute(stmt.values(payload=encode_evidence(replace(evidence, isolated_fixture=True))))
            await _append_evidence(case, encode_evidence(replace(evidence, attempt_id=uuid4())))
        elif damage == "schema":
            value["schema_version"] = True
        elif damage == "forged-verdict":
            value["verdict"] = "pass"
        elif damage.startswith("wrong-"):
            field = damage.removeprefix("wrong-") + "_id"
            plan = replace(evidence.plan, binding=replace(evidence.plan.binding, **{field: uuid4()}))
            observations = replace(evidence.observations, binding=plan.binding, plan_fingerprint=plan.fingerprint)
            value = encode_evidence(replace(evidence, plan=plan, observations=observations))
        elif damage == "result-digest":
            value["result_sha256"] = "0" * 64
        elif damage == "malformed-observation":
            value["observations"][0]["issue"] = "read_error"
        if damage not in ("duplicate-attempt", "attempt-after-terminal", "inconsistent-terminal"):
            async with case.factory.begin() as session:
                await session.execute(update(AgentEventModel).where(AgentEventModel.id == original.id).values(payload=value))
        if damage in ("inconsistent-terminal", "attempt-after-terminal"):
            async with case.factory.begin() as session:
                await session.execute(update(AgentRunModel).where(AgentRunModel.id == run.id).values(status="succeeded"))
        async with _client(_app(case)) as client:
            for response in (
                await _read(client, case.run_id, limit=1),
                await _read(client, case.run_id, limit=1, after_sequence=original.sequence),
                await _read(client, case.run_id, evidence.attempt_id),
            ):
                assert response.status_code == 500 and response.json() == FAILED
                assert FILE not in response.text and evidence.result_sha256 not in response.text

    fixture_run(check)


def test_postgres_evidence_history_cap_is_enforced_even_beyond_cursor(fixture_run):
    async def check(case, _idle):
        evidence = await _inconclusive(case)
        _, _, _, rows = await _snapshot(case.factory, case)
        last = rows[-1].sequence
        async with case.factory.begin() as session:
            session.add_all([
                AgentEventModel(agent_run_id=case.run_id, sequence=seq, event_type=VERIFICATION_RECORDED,
                                payload=encode_evidence(replace(evidence, attempt_id=uuid4())))
                for seq in range(last + 1, last + 512)
            ])
        async with _client(_app(case)) as client:
            full = _data(await _read(client, case.run_id))
            assert len(full["items"]) == 20 and full["has_more"]
            assert _data(await _read(client, case.run_id, evidence.attempt_id))["verdict"] == "inconclusive"
            await _append_evidence(case, encode_evidence(replace(evidence, attempt_id=uuid4())))
            for response in (
                await _read(client, case.run_id, after_sequence=last + 600),
                await _read(client, case.run_id, evidence.attempt_id),
            ):
                assert response.status_code == 500 and response.json() == FAILED

    fixture_run(check)
