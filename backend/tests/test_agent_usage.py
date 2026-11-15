import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.services.agent_usage import (
    AgentUsageSnapshot,
    ModelCallOutcome as Outcome,
    ModelCallStage as Stage,
    ModelCallUsage,
    TokenUsage,
    UsageCollector,
    UsageStatus as Status,
    capture_agent_usage,
    current_usage_collector,
)


def test_usage_records_and_snapshot_are_frozen():
    tokens = TokenUsage(8, 3, 11, cached_input_tokens=4, reasoning_tokens=1)
    call = ModelCallUsage(1, Stage.RESEARCH, Outcome.RETURNED, Status.REPORTED, tokens)
    snapshot = AgentUsageSnapshot(uuid4(), (call,))
    for value, attr in ((tokens, "total_tokens"), (call, "call_number"), (snapshot, "calls")):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, attr, None)


@pytest.mark.parametrize("counts", [
    (True, 0, 1), (0, -1, 0), (0, 1.0, 1), (0, 0, "0"), (1, 2, 4),
    (1, 2, 3, 2), (1, 2, 3, None, True), (1, 2, 3, None, 3),
])
def test_token_usage_rejects_invalid_totals_and_details(counts):
    with pytest.raises(ValueError):
        TokenUsage(*counts)


@pytest.mark.parametrize("outcome,status,tokens", [
    (Outcome.STARTED, Status.REPORTED, TokenUsage(0, 0, 0)),
    (Outcome.ERROR, Status.REPORTED, TokenUsage(0, 0, 0)),
    (Outcome.CANCELLED, Status.INVALID, None),
    (Outcome.RETURNED, Status.REPORTED, None),
    (Outcome.RETURNED, Status.MISSING, TokenUsage(0, 0, 0)),
    (Outcome.RETURNED, Status.INVALID, TokenUsage(0, 0, 0)),
    ("returned", Status.MISSING, None),
])
def test_call_usage_rejects_inconsistent_records(outcome, status, tokens):
    with pytest.raises(ValueError, match="^invalid_call_usage$"):
        ModelCallUsage(1, Stage.RESEARCH, outcome, status, tokens)


def test_collector_preserves_start_order_and_exact_totals():
    run_id = uuid4()
    collector = UsageCollector(run_id)
    first = collector.start_call(Stage.RESEARCH)
    second = collector.start_call(Stage.FINALIZATION)
    before = collector.snapshot()
    assert before.calls == (
        ModelCallUsage(1, Stage.RESEARCH, Outcome.STARTED, Status.MISSING, None),
        ModelCallUsage(2, Stage.FINALIZATION, Outcome.STARTED, Status.MISSING, None),
    )
    assert before.reported_calls == 0 and before.unknown_usage_calls == 2 and not before.usage_complete
    assert isinstance(before.calls, tuple)
    collector.finish_call(second, outcome=Outcome.RETURNED, usage_status=Status.REPORTED, tokens=TokenUsage(0, 0, 0))
    collector.finish_call(first, outcome=Outcome.RETURNED, usage_status=Status.REPORTED, tokens=TokenUsage(7, 5, 12))

    complete = collector.snapshot()
    assert complete.run_id == run_id
    assert [call.call_number for call in complete.calls] == [1, 2]
    assert complete.reported_calls == 2 and complete.unknown_usage_calls == 0 and complete.usage_complete
    assert all(call.outcome is Outcome.STARTED for call in before.calls)
    assert before.known_total_tokens == 0

    for outcome, status in (
        (Outcome.RETURNED, Status.MISSING), (Outcome.RETURNED, Status.INVALID),
        (Outcome.CANCELLED, Status.MISSING), (Outcome.ERROR, Status.MISSING),
    ):
        number = collector.start_call(Stage.RESEARCH)
        collector.finish_call(number, outcome=outcome, usage_status=status, tokens=None)
    collector.start_call(Stage.FINALIZATION)
    partial = collector.snapshot()
    assert partial.model_calls_started == 7
    assert partial.reported_calls == 2 and partial.unknown_usage_calls == 5
    assert (partial.known_input_tokens, partial.known_output_tokens, partial.known_total_tokens) == (7, 5, 12)
    assert not partial.usage_complete


def test_collector_rejects_duplicate_and_unknown_finishes_without_changing_records():
    collector = UsageCollector(uuid4())
    number = collector.start_call(Stage.RESEARCH)
    with pytest.raises(ValueError, match="^invalid_call_outcome$"):
        collector.finish_call(number, outcome=Outcome.STARTED, usage_status=Status.MISSING, tokens=None)
    with pytest.raises(ValueError, match="^unknown_model_call$"):
        collector.finish_call(0, outcome=Outcome.RETURNED, usage_status=Status.MISSING, tokens=None)
    with pytest.raises(ValueError, match="^invalid_call_usage$"):
        collector.finish_call(number, outcome=Outcome.RETURNED, usage_status=Status.REPORTED, tokens=None)
    assert collector.snapshot().calls[0].outcome is Outcome.STARTED
    collector.finish_call(number, outcome=Outcome.RETURNED, usage_status=Status.MISSING, tokens=None)
    before = collector.snapshot()
    with pytest.raises(RuntimeError, match="^model_call_already_finished$"):
        collector.finish_call(number, outcome=Outcome.ERROR, usage_status=Status.MISSING, tokens=None)
    assert collector.snapshot() == before


def test_capture_scope_closes_and_restores_nested_context():
    assert current_usage_collector() is None
    run_id = uuid4()
    with capture_agent_usage(run_id) as outer:
        assert current_usage_collector() is outer
        empty = outer.snapshot()
        assert empty == AgentUsageSnapshot(run_id, ())
        assert empty.model_calls_started == empty.reported_calls == empty.unknown_usage_calls == 0
        assert (empty.known_input_tokens, empty.known_output_tokens, empty.known_total_tokens) == (0, 0, 0)
        assert empty.usage_complete
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with capture_agent_usage(uuid4()) as inner:
                assert current_usage_collector() is inner
                assert inner is not outer
                inner.start_call(Stage.RESEARCH)
                raise RuntimeError("synthetic failure")
        assert inner.closed and current_usage_collector() is outer
        assert outer.snapshot() == empty
    assert outer.closed and current_usage_collector() is None
    assert outer.snapshot() == empty


def test_closed_collector_rejects_late_writes_in_inherited_context():
    async def check():
        release = asyncio.Event()

        async def child():
            await release.wait()
            assert current_usage_collector() is None
            with pytest.raises(RuntimeError, match="^usage_collector_closed$"):
                collector.start_call(Stage.FINALIZATION)
            with pytest.raises(RuntimeError, match="^usage_collector_closed$"):
                collector.finish_call(number, outcome=Outcome.RETURNED, usage_status=Status.MISSING, tokens=None)

        with capture_agent_usage(uuid4()) as collector:
            number = collector.start_call(Stage.RESEARCH)
            task = asyncio.create_task(child())
        before = collector.snapshot()
        release.set()
        await asyncio.wait_for(task, timeout=3)
        assert collector.closed and collector.snapshot() == before

    asyncio.run(check())


def test_cancelled_scope_restores_parent_and_retains_snapshot():
    async def check():
        started = asyncio.Event()
        closed = []

        async def child():
            try:
                with capture_agent_usage(uuid4()) as collector:
                    assert current_usage_collector() is collector
                    collector.start_call(Stage.RESEARCH)
                    started.set()
                    await asyncio.Event().wait()
            finally:
                assert current_usage_collector() is parent
                closed.append(collector)

        with capture_agent_usage(uuid4()) as parent:
            task = asyncio.create_task(child())
            try:
                async with asyncio.timeout(3):
                    await started.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
            assert current_usage_collector() is parent and parent.snapshot().calls == ()
        assert current_usage_collector() is None
        assert len(closed) == 1 and closed[0].closed
        assert closed[0].snapshot().calls[0].outcome is Outcome.STARTED

    asyncio.run(check())
