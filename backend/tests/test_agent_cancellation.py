import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.active_runs import ActiveRunRegistry
from app.services.agent_failure import AgentFailure, AgentFailureCode
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_runs import AgentRunService
from app.services.agent_service import AgentService


READY = AgentFinalResult(
    summary="Synthetic analysis", status="ready_for_verification",
    relevant_components=(), assumptions=(), observations=(), blocked_reason=None,
)
BLOCKED = READY.model_copy(update={"status": "blocked", "blocked_reason": "Synthetic blocker"})


async def _join(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)


@pytest.fixture
def request_snapshot():
    return AgentRunRequest(uuid4(), uuid4(), "Synthetic task", ())


@pytest.fixture
def make_service(monkeypatch, request_snapshot):
    def make(run, *, registry=None, timeout=60):
        registry = ActiveRunRegistry() if registry is None else registry
        service = AgentService(
            None, SimpleNamespace(run=run), active_runs=registry, execution_timeout_seconds=timeout,
        )
        monkeypatch.setattr(service, "_claim", AsyncMock(return_value=request_snapshot))
        monkeypatch.setattr(service, "_complete", AsyncMock())
        monkeypatch.setattr(service, "_fail", AsyncMock())
        return service, registry

    return make


def test_registry_requires_current_task():
    registry = ActiveRunRegistry()
    with pytest.raises(RuntimeError, match="^execution_task_required$"):
        registry.register(uuid4())

    async def check():
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def register_from_callback():
            try:
                registry.register(uuid4())
            except RuntimeError as error:
                done.set_result(error)
            else:
                done.set_result(None)

        loop.call_soon(register_from_callback)
        error = await asyncio.wait_for(done, timeout=3)
        assert isinstance(error, RuntimeError) and str(error) == "execution_task_required"

    asyncio.run(check())


def test_registry_tracks_independent_handles_and_run_ids():
    async def check():
        registry = ActiveRunRegistry()
        run_id, other_id, unknown_id = uuid4(), uuid4(), uuid4()
        first = registry.register(run_id)
        second = registry.register(run_id)
        third = registry.register(run_id)
        other = registry.register(other_id)
        assert first.task is second.task is third.task is other.task is asyncio.current_task()
        assert len({first, second, third}) == 3
        registry.request_cancel(unknown_id)
        assert unknown_id not in registry
        registry.unregister(run_id, first)
        assert run_id in registry
        registry.request_cancel(run_id)
        assert second.cancellation_requested and third.cancellation_requested
        assert not first.cancellation_requested and not other.cancellation_requested
        assert asyncio.current_task().cancelling() == 0
        registry.unregister(run_id, second)
        assert run_id in registry
        registry.unregister(run_id, third)
        assert run_id not in registry and other_id in registry
        registry.unregister(other_id, other)
        assert other_id not in registry

    asyncio.run(check())


def test_registry_cancels_active_task_once():
    async def check():
        registry, run_id = ActiveRunRegistry(), uuid4()
        started, received, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run():
            handle = registry.register(run_id)
            try:
                handle.runner_active = True
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    received.set()
                    await release.wait()
                    raise
            finally:
                registry.unregister(run_id, handle)

        task = asyncio.create_task(run())
        try:
            async with asyncio.timeout(3):
                await started.wait()
                registry.request_cancel(run_id)
                await received.wait()
                registry.request_cancel(run_id)
                assert task.cancelling() == 1
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert run_id not in registry
        finally:
            await _join(task)

    asyncio.run(check())


def test_service_owns_registry_unless_shared_explicitly():
    first, second = AgentService(None, None), AgentService(None, None)
    assert first._active_runs is not second._active_runs
    registry = ActiveRunRegistry()
    assert AgentService(None, None, active_runs=registry)._active_runs is registry


def test_cancellation_during_claim_prevents_runner(make_service, request_snapshot):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        run_id = request_snapshot.run_id

        async def unexpected_run(_request):
            pytest.fail("Cancelled claim proceeded to runner")

        service, registry = make_service(unexpected_run)

        async def claim(_run_id):
            assert run_id in registry
            entered.set()
            await release.wait()
            return request_snapshot

        service._claim.side_effect = claim
        task = asyncio.create_task(service.execute_run(run_id))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                registry.request_cancel(run_id)
                assert task.cancelling() == 0
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert run_id not in registry
            service._complete.assert_not_called()
            service._fail.assert_not_called()
        finally:
            await _join(task)

    asyncio.run(check())


@pytest.mark.parametrize("outcome", ["ready", "blocked", "deadline", "inner-timeout", "error", "claim-error", "persistence-error"])
def test_execution_unregisters_on_exit(make_service, request_snapshot, outcome):
    async def check():
        run_id = request_snapshot.run_id
        original = TimeoutError("synthetic timeout") if outcome == "inner-timeout" else RuntimeError("synthetic failure")
        invoked = []

        async def run(request):
            invoked.append(request)
            assert run_id in registry
            if outcome == "deadline":
                await asyncio.Event().wait()
            if outcome in ("inner-timeout", "error"):
                raise original
            return BLOCKED if outcome == "blocked" else READY

        service, registry = make_service(run, timeout=0.05 if outcome == "deadline" else 60)
        if outcome == "claim-error":
            service._claim.side_effect = original
        if outcome == "persistence-error":
            service._complete.side_effect = original

        async with asyncio.timeout(3):
            if outcome in ("ready", "blocked"):
                result = await service.execute_run(run_id)
                assert result is (READY if outcome == "ready" else BLOCKED)
                service._complete.assert_awaited_once_with(run_id, result)
            else:
                expected = AgentRunnerError if outcome == "deadline" else type(original)
                with pytest.raises(expected) as exc:
                    await service.execute_run(run_id)
                if outcome == "deadline":
                    assert exc.value.reason == "execution_deadline_exceeded"
                    assert isinstance(exc.value.__cause__, TimeoutError)
                else:
                    assert exc.value is original

        codes = {
            "deadline": AgentFailureCode.EXECUTION_DEADLINE_EXCEEDED,
            "inner-timeout": AgentFailureCode.RUNNER_TIMEOUT,
            "error": AgentFailureCode.EXECUTION_FAILED,
        }
        if outcome in codes:
            service._fail.assert_awaited_once_with(run_id, AgentFailure(codes[outcome]))
        else:
            service._fail.assert_not_called()
        assert invoked == ([] if outcome == "claim-error" else [request_snapshot])
        assert run_id not in registry

    asyncio.run(check())


@pytest.mark.parametrize("source,outcome", [
    ("explicit", "result"), ("explicit", "error"),
    ("external", "propagate"), ("external", "result"), ("external", "error"),
])
def test_runner_interruption_never_persists_outcome(make_service, request_snapshot, source, outcome):
    async def check():
        started, cleaned = asyncio.Event(), asyncio.Event()
        run_id = request_snapshot.run_id

        async def run(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if outcome == "propagate":
                    raise
                if source == "explicit":
                    # An explicit request remains observable even if the runner clears the count.
                    asyncio.current_task().uncancel()
                if outcome == "error":
                    raise RuntimeError("synthetic interruption")
                return READY
            finally:
                cleaned.set()

        service, registry = make_service(run)
        task = asyncio.create_task(service.execute_run(run_id))
        try:
            async with asyncio.timeout(3):
                await started.wait()
                if source == "explicit":
                    registry.request_cancel(run_id)
                else:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert cleaned.is_set()
            assert run_id not in registry
            service._complete.assert_not_called()
            service._fail.assert_not_called()
        finally:
            await _join(task)

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["completion", "failure"])
def test_local_signal_does_not_cancel_persistence(make_service, request_snapshot, phase):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        run_id = request_snapshot.run_id
        original = RuntimeError("synthetic execution error")

        async def run(_request):
            if phase == "failure":
                raise original
            return READY

        async def persist(*_args):
            entered.set()
            await release.wait()

        service, registry = make_service(run)
        boundary = service._complete if phase == "completion" else service._fail
        boundary.side_effect = persist
        task = asyncio.create_task(service.execute_run(run_id))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                registry.request_cancel(run_id)
                assert task.cancelling() == 0
                release.set()
                if phase == "completion":
                    assert await task is READY
                else:
                    with pytest.raises(RuntimeError) as exc:
                        await task
                    assert exc.value is original
            boundary.assert_awaited_once()
            assert run_id not in registry
        finally:
            await _join(task)

    asyncio.run(check())


@pytest.mark.parametrize("domain_fails,close_fails", [(False, False), (False, True), (True, False)])
def test_cancel_run_signals_shared_registry_only_after_confirmed_commit(
    make_service, request_snapshot, monkeypatch, domain_fails, close_fails,
):
    async def check():
        started, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        run_id = request_snapshot.run_id
        order = []
        original = RuntimeError("synthetic database error")

        async def run(_request):
            started.set()
            try:
                await release.wait()
                return READY
            except asyncio.CancelledError:
                assert order == ["commit", "close"]
                observed.set()
                raise

        @asynccontextmanager
        async def cancellation_session():
            try:
                yield None
            finally:
                assert task.cancelling() == 0
                order.append("close")
                if close_fails:
                    raise original

        async def cancel_execution(_self, cancelled_id):
            assert cancelled_id == run_id and task.cancelling() == 0
            if domain_fails:
                raise original
            order.append("commit")

        monkeypatch.setattr(AgentRunService, "cancel_execution", cancel_execution)
        executor, registry = make_service(run)
        canceller = AgentService(cancellation_session, None, active_runs=registry)
        task = asyncio.create_task(executor.execute_run(run_id))
        try:
            async with asyncio.timeout(3):
                await started.wait()
                if domain_fails or close_fails:
                    with pytest.raises(RuntimeError) as exc:
                        await canceller.cancel_run(run_id)
                    assert exc.value is original
                else:
                    await canceller.cancel_run(run_id)
                if domain_fails:
                    assert order == ["close"] and task.cancelling() == 0
                    release.set()
                    assert await task is READY
                    assert not observed.is_set()
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert observed.is_set()
                    executor._complete.assert_not_called()
                executor._fail.assert_not_called()
            assert run_id not in registry
        finally:
            await _join(task)

    asyncio.run(check())
