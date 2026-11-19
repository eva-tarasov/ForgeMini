"""Lifespan ownership of one HTTP-started run in a local single-worker app."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import AgentRunStatus
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.schemas.approval import ApprovalPatchReview, LiveApprovalPreview
from app.services.active_runs import ActiveRunRegistry
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunner
from app.services.agent_runs import AgentRunMetadata, AgentRunNotFoundError, AgentRunService
from app.services.agent_service import AgentService, cancel_agent_run
from app.services.approval_contracts import ApprovalDecision
from app.services.approval_preview import (
    ApprovalDecisionConflictError, ApprovalDecisionPort, ApprovalPreviewReader, ApprovalPreviewTarget,
    ApprovalReviewReader,
)


logger = logging.getLogger("forge.agent.http_execution")
SessionFactory = Callable[[], AsyncSession]
_SHUTDOWN_TIMEOUT_SECONDS = 2.0


class RunExecutionUnavailableError(Exception):
    pass


class RunAlreadyStartedError(Exception):
    pass


class RunCapacityExceededError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RunExecutionBinding:
    """Backend-selected workspace and synchronous, side-effect-free runner composition."""

    workspace_id: UUID
    build_runner: Callable[[SessionFactory, UUID], AgentRunner]


@dataclass(frozen=True, slots=True)
class _Execution:
    run_id: UUID
    task: asyncio.Task[AgentFinalResult]
    runner: AgentRunner


class InProcessRunCoordinator:
    def __init__(self, session_factory: SessionFactory, binding: RunExecutionBinding) -> None:
        self._session_factory = session_factory
        self._binding = binding
        self._active_runs = ActiveRunRegistry()
        self._admission = asyncio.Lock()
        self._execution: _Execution | None = None
        self._shutdown_cancel: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self, run_id: UUID) -> None:
        async with self._admission:
            if self._closing:
                raise RunExecutionUnavailableError()
            run = await self._bound_run(run_id)
            if self._closing:
                raise RunExecutionUnavailableError()
            active = self._execution
            occupied = active is not None and not active.task.done()
            if run.status is not AgentRunStatus.QUEUED or (occupied and active.run_id == run_id):
                raise RunAlreadyStartedError()
            if occupied:
                raise RunCapacityExceededError()

            try:
                runner = self._binding.build_runner(self._session_factory, run_id)
                if not callable(getattr(runner, "run", None)):
                    raise TypeError("invalid_runner")
            except Exception:
                raise RunExecutionUnavailableError() from None
            service = AgentService(self._session_factory, runner, active_runs=self._active_runs)
            execution = service.execute_run(run_id)
            try:
                task = asyncio.create_task(execution, name="forge-http-agent-run")
            except BaseException:
                execution.close()
                raise
            # No suspension between creating the task and handing ownership to the app.
            self._execution = _Execution(run_id, task, runner)
            task.add_done_callback(self._observe_completion)

    async def cancel(self, run_id: UUID) -> AgentRunMetadata:
        await self._bound_run(run_id)
        await cancel_agent_run(self._session_factory, self._active_runs, run_id)
        async with self._session_factory() as session:
            return await AgentRunService(session).get_metadata(run_id)

    def _active_runner_for(self, target: ApprovalPreviewTarget) -> AgentRunner | None:
        active = self._execution
        if (
            self._closing or active is None or active.task.done() or active.task.cancelling()
            or active.run_id != target.run_id or self._binding.workspace_id != target.workspace_id
        ):
            return None
        return active.runner

    def read_approval_preview(self, target: ApprovalPreviewTarget) -> LiveApprovalPreview | None:
        runner = self._active_runner_for(target)
        if isinstance(runner, ApprovalPreviewReader):
            return runner.read_approval_preview(target)
        return None

    def read_approval_review(
        self, target: ApprovalPreviewTarget, request_events: Sequence[object],
    ) -> ApprovalPatchReview | None:
        runner = self._active_runner_for(target)
        if isinstance(runner, ApprovalReviewReader):
            return runner.read_approval_review(target, request_events)
        return None

    async def decide_approval(
        self, target: ApprovalPreviewTarget, request_events: Sequence[object],
        decision: ApprovalDecision, *, reviewed_patch_sha256: str | None,
    ) -> None:
        runner = self._active_runner_for(target)
        if not isinstance(runner, ApprovalDecisionPort):
            raise ApprovalDecisionConflictError()
        await runner.decide_approval(target, request_events, decision, reviewed_patch_sha256=reviewed_patch_sha256)

    async def _bound_run(self, run_id: UUID) -> AgentRunMetadata:
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            task = await TaskRepository(session).get(run.task_id)
            if task is None or task.workspace_id != self._binding.workspace_id:
                raise RunExecutionUnavailableError()
            return AgentRunMetadata.from_record(run)

    def _observe_completion(self, task: asyncio.Task[AgentFinalResult]) -> None:
        if self._execution is not None and self._execution.task is task:
            self._execution = None
        if not task.cancelled() and task.exception() is not None:
            logger.warning("http_agent_run_execution_failed")

    def _observe_shutdown_cancel(self, task: asyncio.Task[None]) -> None:
        self._shutdown_cancel = None
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closing = True
        active = self._execution
        if active is None or active.task.done():
            return
        cancellation = asyncio.create_task(
            cancel_agent_run(self._session_factory, self._active_runs, active.run_id),
            name="forge-http-shutdown-cancel",
        )
        self._shutdown_cancel = cancellation
        cancellation.add_done_callback(self._observe_shutdown_cancel)
        settled, _ = await asyncio.wait({cancellation}, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        if not settled:
            cancellation.cancel()
        if not settled or cancellation.cancelled() or cancellation.exception() is not None:
            logger.warning("http_agent_run_shutdown_cancel_unconfirmed")
            active.task.cancel()
        done, _ = await asyncio.wait({active.task}, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        if not done:
            active.task.cancel()
            done, _ = await asyncio.wait({active.task}, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        if not done:
            logger.warning("http_agent_run_shutdown_execution_pending")
        if not cancellation.done():
            logger.warning("http_agent_run_shutdown_persistence_pending")
