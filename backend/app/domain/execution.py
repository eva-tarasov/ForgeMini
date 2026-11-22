"""AgentRun status values, without transition behavior (the graph lives in domain/transitions.py)."""

from enum import Enum


class AgentRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

