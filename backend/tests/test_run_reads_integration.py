"""Public read projections (events, result, usage) from real PostgreSQL: fail-closed, no echo of stored private data.

Ported in ADR-003 (C3) from the M1-era `test_agent_runs_read_integration`. Kept: cursor/keyset paging, unknown events,
result sections, usage decimals, fail-closed on corrupt or contradictory storage. Dropped with their subject: the M1
verification-evidence attempts and tool/approval event validation. NEW: stored M1-era events now project as "other".
Opt-in like the other task06 integration tests (FORGE_TASK06_TEST_DB + POSTGRES_DB).
"""

import os
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete

from app.db.models import AgentEventModel, AgentRunModel, TaskModel
from app.main import create_app
from app.repositories.agent_events import AgentEventRepository
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_runs import AgentRunService
from app.services.agent_usage import (
    AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, TokenUsage, UsageStatus,
)
from app.services.agent_usage_codec import AgentUsageRecord
from run_harness import make_run_scenario

from contextlib import asynccontextmanager

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None,
    reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to the dedicated forge_task06_* database",
)
PRIVATE = "synthetic-private /host/root file contents and patch"
READY = AgentFinalResult(
    summary=PRIVATE, status="ready_for_verification", relevant_components=(PRIVATE,), assumptions=(PRIVATE,),
    observations=(FixtureObservation(component="task_service", tool_call_id=PRIVATE, content=PRIVATE),),
    blocked_reason=None,
)


@pytest.fixture
def run_scenario():
    return make_run_scenario()


@asynccontextmanager
async def _client(factory):
    app = create_app(session_factory=factory)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://forge-tests.invalid", trust_env=False,
    ) as client:
        yield client


async def _snapshot(factory, case):
    async with factory() as session:
        return await session.get(AgentRunModel, case.run_id), await session.get(TaskModel, case.task_id)


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



def test_postgres_empty_page_preserves_cursor_and_missing_run_is_404(run_scenario):
    async def check(case):
        async with _client(case.factory) as client:
            url = _url(case.run_id, "events")
            for cursor in (0, 2147483647):
                response = await client.get(url, params={"after_sequence": cursor})
                assert response.status_code == 200
                assert response.json() == {"items": [], "next_after_sequence": cursor, "has_more": False}
                await case.idle()
            for suffix in ("events", "result"):
                response = await client.get(_url(uuid4(), suffix))
                assert response.status_code == 404 and response.json()["error"]["code"] == "AGENT_RUN_NOT_FOUND"
    run_scenario(check)


@pytest.mark.parametrize("limit", [1, 2])
def test_postgres_keyset_traverses_unknown_and_known_events_without_skips(run_scenario, limit):
    async def check(case):
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
                    await case.idle()
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
    run_scenario(check)


def test_postgres_poll_sees_new_events_only_after_commit_and_closes_sessions(run_scenario):
    async def check(case):
        await _append(case.factory, case.run_id, ("known-only-to-backend", {}))
        async with _client(case.factory) as client:
            async with case.factory.begin() as writer:
                await AgentEventRepository(writer).append(case.run_id, event_type="future", payload={"raw": PRIVATE})
                await writer.flush()
                response = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
                assert response.json() == {"items": [], "next_after_sequence": 1, "has_more": False}
            await case.idle()
            response = await client.get(_url(case.run_id, "events"), params={"after_sequence": 1})
            [item] = _items(response)
            assert item["sequence"] == 2 and item["kind"] == "other" and item["details"] == {}
            _assert_private_absent(response)
            await case.idle()
    run_scenario(check)


@pytest.mark.parametrize("status", ["queued", "running", "cancelled"])
def test_postgres_result_before_preliminary_contains_null_sections(run_scenario, status):
    async def check(case):
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
        await case.idle()
        run, task = await _snapshot(case.factory, case)
        assert (run.status, task.status) == (status, "queued")
    run_scenario(check)


@pytest.mark.parametrize("outcome", ["ready", "blocked", "failure"])
def test_postgres_preliminary_and_execution_failure_are_separate_safe_projections(run_scenario, outcome):
    async def check(case):
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
    run_scenario(check)




def _usage_record(case, mode):
    tariff = ModelTariff("synthetic-private-provider", "synthetic-private-model", "v1",
                         Decimal("0.000001"), Decimal("0.000002"), Decimal("0.000001"))
    cached = 0 if mode == "uncached-known-split" else None     # None = the provider did not report the cache split

    def reported(i, stage):
        tokens = TokenUsage(1, 1, 2, cached_input_tokens=cached)
        return ModelCallUsage(i, stage, ModelCallOutcome.RETURNED, UsageStatus.REPORTED, tokens)
    calls = [reported(1, ModelCallStage.RESEARCH)]
    if mode in ("partial", "unpriced"):
        calls.extend([
            ModelCallUsage(2, ModelCallStage.FINALIZATION, ModelCallOutcome.RETURNED, UsageStatus.INVALID, None),
            ModelCallUsage(3, ModelCallStage.RESEARCH, ModelCallOutcome.RETURNED, UsageStatus.MISSING, None),
        ])
    else:
        calls.append(reported(2, ModelCallStage.FINALIZATION))
    if mode.startswith("uncached"):     # provider publishes no cached-input price: stored as null, must decode back
        tariff = ModelTariff("synthetic-private-provider", "synthetic-private-model", "v1",
                             Decimal("0.000001"), Decimal("0.000002"), None)
    pricing = AgentPricing(None if mode == "unpriced" else tariff, tariff)
    return AgentUsageRecord(AgentUsageSnapshot(case.run_id, tuple(calls)), pricing)


@pytest.mark.parametrize("mode", ["complete", "partial", "unpriced", "uncached-known-split", "uncached-unknown-split"])
def test_postgres_usage_preserves_exact_decimal_and_incompleteness(run_scenario, mode):
    async def check(case):
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
                "known_total_tokens": 2 if mode in ("partial", "unpriced") else 4,
                "usage_complete": mode not in ("partial", "unpriced"),
                "known_cost_usd": {
                    "complete": "0.000000000006", "uncached-known-split": "0.000000000006",
                    # unknown cache split + no cached price: Forge refuses to guess a cost (CACHE_PRICING_UNKNOWN)
                    "uncached-unknown-split": None, "partial": "0.000000000003", "unpriced": None,
                }[mode],
                "cost_complete": mode in ("complete", "uncached-known-split"),
            }
            events = await client.get(_url(case.run_id, "events"))
            assert [item["details"] for item in _items(events) if item["kind"] == "agent_usage_recorded"] == [{}]
            _assert_private_absent(response)
            _assert_private_absent(events)
    run_scenario(check)


@pytest.mark.parametrize("kind", [
    "agent_run_status_changed", "agent_result_recorded", "agent_execution_failed", "agent_usage_recorded",
    "agent_step", "sandbox_verification_recorded",
])
def test_postgres_malformed_known_event_fails_closed_without_echo(run_scenario, kind):
    async def check(case):
        await _append(case.factory, case.run_id, (kind, {"schema_version": True, "private": PRIVATE}))
        async with _client(case.factory) as client:
            _assert_safe_failure(await client.get(_url(case.run_id, "events")))
            if kind in ("agent_execution_failed", "agent_usage_recorded"):
                _assert_safe_failure(await client.get(_url(case.run_id, "result")))
    run_scenario(check)


@pytest.mark.parametrize("corruption", ["invalid-result", "succeeded-without-evidence", "duplicate-failure"])
def test_postgres_result_fails_closed_on_corrupt_or_contradictory_snapshot(run_scenario, corruption):
    async def check(case):
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
    run_scenario(check)




def test_stored_m1_era_events_project_as_other_with_empty_details(run_scenario):
    """Owner decision (ADR-003 Q1): tool_call_* / approval_* left the public contract and the stored M1 evidence kind verification_recorded is no longer read.
    Old rows must still READ (as "other", no payload echoed), not crash the events endpoint."""
    async def check(case):
        entries = [(kind, {"schema_version": 1, "raw": PRIVATE}) for kind in (
            "tool_call_proposed", "tool_call_started", "tool_call_finished", "approval_requested", "approval_decided",
            "verification_recorded")]
        await _append(case.factory, case.run_id, *entries)
        async with _client(case.factory) as client:
            response = await client.get(_url(case.run_id, "events"))
            assert [(i["kind"], i["details"]) for i in _items(response)] == [("other", {})] * len(entries)
            _assert_private_absent(response)
    run_scenario(check)


def test_result_history_limit_fails_closed_exactly_at_the_bound(run_scenario, monkeypatch):
    """The bounded snapshot: N result events are readable, N+1 fail closed (never an unbounded read).

    The run is VALID and consistent (failure + usage events), so the ONLY thing that differs between the two reads is
    the limit itself. The real bound is 512; it is patched to 2/1 because 512 distinct valid events cannot exist
    (a second failure or usage event is a contradiction and fails closed for a different reason)."""
    from app.services import agent_run_reads

    async def check(case):
        async with case.factory() as session:
            service = AgentRunService(session)
            await service.claim_for_execution(case.run_id)
            await service.fail_execution(case.run_id, AgentFailure(AgentFailureCode.PROVIDER_AUTH))
            await service.record_usage(_usage_record(case, "complete"))
        async with _client(case.factory) as client:
            monkeypatch.setattr(agent_run_reads, "MAX_RESULT_EVENTS", 2)
            at_limit = await client.get(_url(case.run_id, "result"))
            assert at_limit.status_code == 200
            assert at_limit.json()["execution_failure_code"] == "provider_auth"
            assert at_limit.json()["usage"]["usage_complete"] is True
            monkeypatch.setattr(agent_run_reads, "MAX_RESULT_EVENTS", 1)
            _assert_safe_failure(await client.get(_url(case.run_id, "result")))
            await case.idle()
            feed = await client.get(_url(case.run_id, "events"))     # the event feed has no such bound: it pages
            assert [i["kind"] for i in _items(feed)] == [
                "agent_run_status_changed", "agent_execution_failed", "agent_run_status_changed",
                "agent_usage_recorded",
            ]
    run_scenario(check)
