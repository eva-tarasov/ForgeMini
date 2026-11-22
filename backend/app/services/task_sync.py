"""Keep Task.status in step with its AgentRun.

M1 never changed Task.status after creation, so `GET /tasks/{id}` said "queued" forever.
Rule kept from M1: Task SUCCEEDED requires independent verification evidence, so only the
verifier passes `verified=True`; every other caller can only move a task to FAILED/CANCELLED.
"""
from app.db.models import TaskModel
from app.domain.execution import AgentRunStatus
from app.domain.task import TaskStatus
from app.domain.task_transitions import validate_task_transition

_TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
_RUN_TO_TASK = {
    AgentRunStatus.SUCCEEDED: TaskStatus.SUCCEEDED,
    AgentRunStatus.FAILED: TaskStatus.FAILED,
    AgentRunStatus.CANCELLED: TaskStatus.CANCELLED,
}


def sync_task_to_run(task: TaskModel, run_status: AgentRunStatus, *, verified: bool = False) -> bool:
    """Stage the Task transition on an already locked row. Returns True if the status changed."""
    target = _RUN_TO_TASK.get(run_status)
    current = TaskStatus(task.status)
    if target is None or current in _TERMINAL:
        return False                          # run still in progress, or task already final: nothing to do
    if target is TaskStatus.SUCCEEDED:
        if not verified:
            raise PermissionError("task_success_requires_verification")
        # QUEUED -> RUNNING -> VERIFYING -> SUCCEEDED, each step checked against the transition graph
        for step in (TaskStatus.RUNNING, TaskStatus.VERIFYING):
            if current is TaskStatus.QUEUED or (current is TaskStatus.RUNNING and step is TaskStatus.VERIFYING):
                validate_task_transition(current, step)
                current = step
    validate_task_transition(current, target)
    task.status = target.value
    return True
