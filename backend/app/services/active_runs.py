import asyncio
from dataclasses import dataclass
from uuid import UUID


@dataclass(eq=False, slots=True)
class ActiveExecution:
    task: asyncio.Task
    cancellation_requested: bool = False
    runner_active: bool = False

    def request_cancel(self) -> None:
        if self.cancellation_requested:
            return
        self.cancellation_requested = True
        if self.runner_active and not self.task.done():
            self.task.cancel()


class ActiveRunRegistry:
    """Track local execution attempts in one process and one event loop."""

    def __init__(self) -> None:
        self._executions: dict[UUID, set[ActiveExecution]] = {}

    def __contains__(self, run_id: UUID) -> bool:
        return run_id in self._executions

    def register(self, run_id: UUID) -> ActiveExecution:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            raise RuntimeError("execution_task_required") from None
        if task is None:
            raise RuntimeError("execution_task_required")
        handle = ActiveExecution(task)
        self._executions.setdefault(run_id, set()).add(handle)
        return handle

    def unregister(self, run_id: UUID, handle: ActiveExecution) -> None:
        handles = self._executions.get(run_id)
        if handles is not None:
            handles.discard(handle)
            if not handles:
                del self._executions[run_id]

    def request_cancel(self, run_id: UUID) -> None:
        for handle in self._executions.get(run_id, ()):
            handle.request_cancel()
