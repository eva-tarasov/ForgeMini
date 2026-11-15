import asyncio
import logging
import math
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from time import perf_counter_ns
from uuid import UUID, uuid4

from app.core.logging import configure_agent_logger
from app.domain.transitions import InvalidAgentRunTransition
from app.services.agent_cost import AgentPricing, estimate_agent_cost
from app.services.agent_failure import AgentFailureCode
from app.services.agent_usage import AgentUsageSnapshot


class AgentOperation(StrEnum):
    EXECUTE = "execute"
    CANCEL = "cancel"


class AgentPhase(StrEnum):
    CLAIM = "claim"
    RUNNER = "runner"
    RESULT_VALIDATION = "result_validation"
    RESULT_PERSISTENCE = "result_persistence"
    FAILURE_PERSISTENCE = "failure_persistence"
    USAGE_PERSISTENCE = "usage_persistence"
    CANCELLATION_PERSISTENCE = "cancellation_persistence"
    CANCELLATION_SIGNAL = "cancellation_signal"


class OperationOutcome(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class SafeErrorKind(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    STALE_RUN_STATUS = "stale_run_status"
    INVALID_TRANSITION = "invalid_transition"
    OPERATION_ERROR = "operation_error"


class _Event(StrEnum):
    OPERATION_STARTED = "agent_operation_started"
    OPERATION_FINISHED = "agent_operation_finished"
    PHASE_STARTED = "agent_phase_started"
    PHASE_FINISHED = "agent_phase_finished"
    FAILURE_CLASSIFIED = "agent_failure_classified"
    USAGE_OBSERVED = "agent_usage_observed"


_COMMON = {"event", "operation_id", "operation", "run_id", "task_id"}
_FINISH = {"outcome", "duration_ms", "error_kind"}
_USAGE_COUNTERS = {
    "model_calls_started", "reported_calls", "unknown_usage_calls",
    "known_input_tokens", "known_output_tokens", "known_total_tokens",
    "priced_calls", "unknown_cost_calls",
}
_EXTRA_FIELDS = {
    _Event.OPERATION_STARTED: set(),
    _Event.OPERATION_FINISHED: _FINISH,
    _Event.PHASE_STARTED: {"phase"},
    _Event.PHASE_FINISHED: _FINISH | {"phase"},
    _Event.FAILURE_CLASSIFIED: {"failure_code"},
    _Event.USAGE_OBSERVED: _USAGE_COUNTERS | {"usage_complete", "known_cost_usd", "cost_complete"},
}


def _event_level(event: _Event, outcome: object) -> int:
    if event is _Event.FAILURE_CLASSIFIED or outcome is OperationOutcome.ERROR:
        return logging.WARNING
    return logging.INFO


def _uuid_string(value: object) -> str:
    # asyncpg returns a UUID subclass. Serialize its numeric UUID value.
    if not issubclass(type(value), UUID):
        raise ValueError("invalid_agent_log_id")
    number = value.int
    if type(number) is not int:
        raise ValueError("invalid_agent_log_id")
    return str(UUID(int=number))


def agent_log_fields(level: str, fields: object) -> dict:
    """Validate the event before passing its fields to the JSON encoder."""
    if type(fields) is not dict or type(fields.get("event")) is not _Event:
        raise ValueError("invalid_agent_log_event")
    event = fields["event"]
    extra = _EXTRA_FIELDS[event]
    if any(type(name) is not str for name in fields) or fields.keys() != _COMMON | extra:
        raise ValueError("invalid_agent_log_fields")
    if type(fields["operation"]) is not AgentOperation:
        raise ValueError("invalid_agent_log_context")
    if "phase" in extra and type(fields["phase"]) is not AgentPhase:
        raise ValueError("invalid_agent_log_phase")
    if "outcome" in extra:
        outcome, duration, kind = fields["outcome"], fields["duration_ms"], fields["error_kind"]
        if (
            type(outcome) is not OperationOutcome
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0
            or (type(kind) is not SafeErrorKind if outcome is OperationOutcome.ERROR else kind is not None)
        ):
            raise ValueError("invalid_agent_log_outcome")
    expected_level = "WARNING" if _event_level(event, fields.get("outcome")) == logging.WARNING else "INFO"
    if type(level) is not str or level != expected_level:
        raise ValueError("invalid_agent_log_level")
    if event is _Event.FAILURE_CLASSIFIED and type(fields["failure_code"]) is not AgentFailureCode:
        raise ValueError("invalid_agent_log_failure_code")

    result = {**fields, "operation_id": _uuid_string(fields["operation_id"]), "run_id": _uuid_string(fields["run_id"])}
    if fields["task_id"] is not None:
        result["task_id"] = _uuid_string(fields["task_id"])
    if event is _Event.USAGE_OBSERVED:
        cost = fields["known_cost_usd"]
        if (
            any(type(fields[name]) is not int or fields[name] < 0 for name in _USAGE_COUNTERS)
            or type(fields["usage_complete"]) is not bool or type(fields["cost_complete"]) is not bool
            or type(cost) is not Decimal or not cost.is_finite() or cost.is_signed()
        ):
            raise ValueError("invalid_agent_log_usage")
        result["known_cost_usd"] = format(cost, "f")
    return result


def _error_kind(error: BaseException) -> SafeErrorKind:
    from app.services.agent_runs import AgentRunNotFoundError, StaleAgentRunStatusError

    if isinstance(error, AgentRunNotFoundError):
        return SafeErrorKind.RUN_NOT_FOUND
    if isinstance(error, StaleAgentRunStatusError):
        return SafeErrorKind.STALE_RUN_STATUS
    if isinstance(error, InvalidAgentRunTransition):
        return SafeErrorKind.INVALID_TRANSITION
    return SafeErrorKind.OPERATION_ERROR


@dataclass(slots=True)
class AgentLogContext:
    run_id: UUID
    operation: AgentOperation
    operation_id: UUID = field(default_factory=uuid4, init=False)
    task_id: UUID | None = None


class AgentObserver:
    def __init__(self, context: AgentLogContext) -> None:
        self.context = context

    def _emit(self, event: _Event, **fields: object) -> None:
        try:
            context = self.context
            logger = configure_agent_logger()
            logger.log(
                _event_level(event, fields.get("outcome")),
                event.value,
                extra={"agent_fields": {
                    "event": event,
                    "operation_id": context.operation_id,
                    "operation": context.operation,
                    "run_id": context.run_id,
                    "task_id": context.task_id,
                    **fields,
                }},
            )
        except Exception:
            # A logging failure must not alter the invocation or report itself recursively.
            pass

    @contextmanager
    def _observe(self, start: _Event, finish: _Event, **fields: object) -> Iterator[None]:
        try:
            started = perf_counter_ns()
        except Exception:
            started = None
        self._emit(start, **fields)
        outcome = OperationOutcome.COMPLETED
        failure = None
        try:
            yield
        except asyncio.CancelledError:
            outcome = OperationOutcome.INTERRUPTED
            raise
        except BaseException as error:
            outcome = OperationOutcome.ERROR
            failure = error
            raise
        finally:
            try:
                if started is not None:
                    self._emit(
                        finish, **fields, outcome=outcome,
                        duration_ms=max(0, perf_counter_ns() - started) / 1_000_000,
                        error_kind=_error_kind(failure) if failure is not None else None,
                    )
            except Exception:
                pass

    def operation(self) -> AbstractContextManager[None]:
        return self._observe(_Event.OPERATION_STARTED, _Event.OPERATION_FINISHED)

    def phase(self, phase: AgentPhase) -> AbstractContextManager[None]:
        return self._observe(_Event.PHASE_STARTED, _Event.PHASE_FINISHED, phase=phase)

    def failure_classified(self, failure_code: AgentFailureCode) -> None:
        self._emit(_Event.FAILURE_CLASSIFIED, failure_code=failure_code)

    def usage_observed(self, snapshot: AgentUsageSnapshot, pricing: AgentPricing) -> None:
        try:
            cost = estimate_agent_cost(snapshot, pricing)
            self._emit(
                _Event.USAGE_OBSERVED,
                model_calls_started=snapshot.model_calls_started,
                reported_calls=snapshot.reported_calls,
                unknown_usage_calls=snapshot.unknown_usage_calls,
                known_input_tokens=snapshot.known_input_tokens,
                known_output_tokens=snapshot.known_output_tokens,
                known_total_tokens=snapshot.known_total_tokens,
                usage_complete=snapshot.usage_complete,
                priced_calls=cost.priced_calls,
                unknown_cost_calls=cost.unknown_cost_calls,
                known_cost_usd=cost.known_cost_usd,
                cost_complete=cost.cost_complete,
            )
        except Exception:
            pass
