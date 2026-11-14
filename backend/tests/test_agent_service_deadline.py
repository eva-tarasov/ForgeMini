import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.agent_service import AgentService, DEFAULT_EXECUTION_TIMEOUT_SECONDS


@pytest.fixture
def request_snapshot():
    return AgentRunRequest(uuid4(), uuid4(), "Synthetic task", ("One criterion",))


@pytest.fixture
def ready_result():
    return AgentFinalResult(
        summary="Synthetic analysis",
        status="ready_for_verification",
        relevant_components=(),
        assumptions=(),
        observations=(),
        blocked_reason=None,
    )


@pytest.fixture
def make_service(monkeypatch, request_snapshot, ready_result):
    def create(*, run=None, **configuration):
        runner = SimpleNamespace(run=AsyncMock(side_effect=run, return_value=ready_result))
        session_factory = Mock(side_effect=AssertionError("Unexpected database session"))
        service = AgentService(session_factory, runner, **configuration)
        monkeypatch.setattr(service, "_claim", AsyncMock(return_value=request_snapshot))
        monkeypatch.setattr(service, "_fail", AsyncMock())
        monkeypatch.setattr(service, "_complete", AsyncMock())
        return service, runner

    return create


def test_agent_service_deadline_defaults_to_sixty_seconds(make_service):
    service, _ = make_service()
    assert service._execution_timeout_seconds == DEFAULT_EXECUTION_TIMEOUT_SECONDS == 60.0


@pytest.mark.parametrize("seconds", [1, 0.25, 180.0])
def test_agent_service_deadline_accepts_positive_finite_numbers(make_service, seconds):
    service, _ = make_service(execution_timeout_seconds=seconds)
    assert type(service._execution_timeout_seconds) is float
    assert service._execution_timeout_seconds == seconds


@pytest.mark.parametrize("seconds", [
    0, -1, float("nan"), float("inf"), float("-inf"), True, False, None, "1",
    pytest.param(10**1000, id="integer-overflow"),
])
def test_agent_service_deadline_rejects_invalid_configuration(seconds):
    session_factory = Mock()
    runner = SimpleNamespace(run=AsyncMock())
    with pytest.raises(ValueError, match="^invalid_execution_timeout$"):
        AgentService(session_factory, runner, execution_timeout_seconds=seconds)
    session_factory.assert_not_called()
    runner.run.assert_not_called()


def test_agent_service_deadline_returns_on_time_result_after_completion(make_service, request_snapshot, ready_result):
    service, runner = make_service(execution_timeout_seconds=5)
    assert asyncio.run(service.execute_run(request_snapshot.run_id)) is ready_result
    service._claim.assert_awaited_once_with(request_snapshot.run_id)
    runner.run.assert_awaited_once_with(request_snapshot)
    service._complete.assert_awaited_once_with(request_snapshot.run_id, ready_result)
    service._fail.assert_not_called()


@pytest.mark.parametrize("swallow_cancellation", [False, True], ids=["propagates", "returns-late"])
def test_agent_service_deadline_cancels_runner_and_rejects_late_result(
    make_service, request_snapshot, ready_result, swallow_cancellation,
):
    async def check():
        waiting = asyncio.Event()
        lifecycle = []

        async def run(_request):
            lifecycle.append("started")
            try:
                await waiting.wait()
            except asyncio.CancelledError:
                lifecycle.append("cancelled")
                if swallow_cancellation:
                    return ready_result
                raise
            finally:
                lifecycle.append("cleanup")

        service, runner = make_service(run=run, execution_timeout_seconds=0.05)

        async def fail(_run_id):
            assert lifecycle == ["started", "cancelled", "cleanup"]

        service._fail.side_effect = fail
        async with asyncio.timeout(3):
            with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$") as exc:
                await service.execute_run(request_snapshot.run_id)
        assert exc.value.reason == "execution_deadline_exceeded"
        if swallow_cancellation:
            assert exc.value.__cause__ is None
        else:
            assert isinstance(exc.value.__cause__, TimeoutError)
            assert isinstance(exc.value.__cause__.__cause__, asyncio.CancelledError)
        service._claim.assert_awaited_once_with(request_snapshot.run_id)
        runner.run.assert_awaited_once_with(request_snapshot)
        service._fail.assert_awaited_once_with(request_snapshot.run_id)
        service._complete.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("wrapped", [False, True], ids=["inner-timeout", "runner-error-with-timeout-cause"])
def test_agent_service_deadline_distinguishes_inner_timeout(make_service, request_snapshot, wrapped):
    inner = TimeoutError("Synthetic runner timeout")
    error = AgentRunnerError("execution_failed") if wrapped else inner

    async def run(_request):
        if wrapped:
            raise error from inner
        raise error

    service, runner = make_service(run=run, execution_timeout_seconds=5)
    with pytest.raises(type(error)) as exc:
        asyncio.run(service.execute_run(request_snapshot.run_id))
    assert exc.value is error
    assert exc.value.__cause__ is (inner if wrapped else None)
    runner.run.assert_awaited_once_with(request_snapshot)
    service._fail.assert_awaited_once_with(request_snapshot.run_id)
    service._complete.assert_not_called()


def test_agent_service_deadline_preserves_external_cancellation(make_service, request_snapshot):
    async def check():
        started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(_request):
            started.set()
            try:
                await release.wait()
            finally:
                cleaned.set()

        service, runner = make_service(run=run, execution_timeout_seconds=60)
        task = asyncio.create_task(service.execute_run(request_snapshot.run_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=3)
            assert task in done
            with pytest.raises(asyncio.CancelledError):
                task.result()
            assert cleaned.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        runner.run.assert_awaited_once_with(request_snapshot)
        service._fail.assert_not_called()
        service._complete.assert_not_called()

    asyncio.run(check())


def test_agent_service_deadline_rejects_late_return_before_timer_callback(
    make_service, request_snapshot, ready_result, monkeypatch,
):
    async def check():
        loop = asyncio.get_running_loop()
        current_time = loop.time()
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: current_time)

            async def run(_request):
                nonlocal current_time
                current_time += 2
                # Return without yielding, so the event loop has not run the timeout callback.
                return ready_result

            service, runner = make_service(run=run, execution_timeout_seconds=1)
            with pytest.raises(AgentRunnerError, match="^execution_deadline_exceeded$") as exc:
                await service.execute_run(request_snapshot.run_id)
        assert exc.value.reason == "execution_deadline_exceeded"
        assert exc.value.__cause__ is None
        runner.run.assert_awaited_once_with(request_snapshot)
        service._fail.assert_awaited_once_with(request_snapshot.run_id)
        service._complete.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("persistence_fails", [False, True])
def test_agent_service_deadline_excludes_claim_and_completion(
    make_service, request_snapshot, ready_result, monkeypatch, persistence_fails,
):
    async def check():
        loop = asyncio.get_running_loop()
        current_time = loop.time()
        clock_origin = current_time
        context = None
        make_timeout = asyncio.timeout_at
        persistence_error = TimeoutError("Synthetic persistence timeout")

        def capture_timeout(deadline):
            nonlocal context
            context = make_timeout(deadline)
            return context

        async def claim(_run_id):
            nonlocal current_time
            assert context is None
            current_time += 10
            await asyncio.sleep(0)
            return request_snapshot

        async def run(_request):
            assert context.when() == clock_origin + 11
            return ready_result

        async def complete(_run_id, _result):
            nonlocal current_time
            current_time += 10
            await asyncio.sleep(0)
            assert not context.expired()
            if persistence_fails:
                raise persistence_error

        service, runner = make_service(run=run, execution_timeout_seconds=1)
        service._claim.side_effect = claim
        service._complete.side_effect = complete
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: current_time)
            patch.setattr(asyncio, "timeout_at", Mock(side_effect=capture_timeout))
            if persistence_fails:
                with pytest.raises(TimeoutError) as exc:
                    await service.execute_run(request_snapshot.run_id)
                assert exc.value is persistence_error
            else:
                assert await service.execute_run(request_snapshot.run_id) is ready_result
            asyncio.timeout_at.assert_called_once_with(clock_origin + 11)
        service._claim.assert_awaited_once_with(request_snapshot.run_id)
        runner.run.assert_awaited_once_with(request_snapshot)
        service._complete.assert_awaited_once_with(request_snapshot.run_id, ready_result)
        service._fail.assert_not_called()

    asyncio.run(check())
