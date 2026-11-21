"""Bounded, read-only AgentRun observation."""

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Literal
from uuid import UUID

from forge_cli.client import ForgeAPIClient
from forge_cli.dto import AgentRunEventsResponse, AgentRunStatus

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class WatchOutcome:
    last_observed_status: AgentRunStatus | None
    next_after_sequence: int
    stop_reason: Literal["terminal", "waiting_approval", "timeout", "interrupted"]


def watch_run(
    client: ForgeAPIClient,
    run_id: UUID,
    *,
    interval: int,
    timeout: int,
    after_sequence: int,
    on_page: Callable[[AgentRunStatus, AgentRunEventsResponse], None],
    clock: Callable[[], float] = monotonic,
    pause: Callable[[float], None] = sleep,
) -> WatchOutcome:
    """Observe through a caller-owned client using already validated options."""
    status: AgentRunStatus | None = None
    cursor = after_sequence
    drain_reason: Literal["terminal", "waiting_approval"] | None = None
    # Requests and buffered JSON events stay bounded independently of clock progress.
    max_cycles = (timeout + interval - 1) // interval
    try:
        deadline = clock() + timeout
        for _ in range(max_cycles):
            if clock() >= deadline:
                break
            status = client.get_run(run_id).status
            if clock() >= deadline:
                break
            page = client.get_run_events(run_id, after_sequence=cursor, limit=50)
            if clock() >= deadline:
                break

            on_page(status, page)
            cursor = page.next_after_sequence
            remaining = deadline - clock()
            if remaining <= 0:
                break
            # Remember a stop state even if the run changes while draining.
            if status in _TERMINAL_STATES:
                drain_reason = "terminal"
            elif status == "waiting_approval" and drain_reason is None:
                drain_reason = "waiting_approval"
            if drain_reason is not None and not page.has_more:
                return WatchOutcome(status, cursor, drain_reason)
            pause(min(interval, remaining))
    except KeyboardInterrupt:
        return WatchOutcome(status, cursor, "interrupted")

    return WatchOutcome(status, cursor, "timeout")
