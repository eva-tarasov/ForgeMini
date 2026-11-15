from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ModelCallStage(StrEnum):
    RESEARCH = "research"
    FINALIZATION = "finalization"


class ModelCallOutcome(StrEnum):
    STARTED = "started"
    RETURNED = "returned"
    ERROR = "error"
    CANCELLED = "cancelled"


class UsageStatus(StrEnum):
    REPORTED = "reported"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (
            self.input_tokens, self.output_tokens, self.total_tokens,
        )):
            raise ValueError("invalid_token_count")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("inconsistent_token_total")
        for value, limit in (
            (self.cached_input_tokens, self.input_tokens), (self.reasoning_tokens, self.output_tokens),
        ):
            if value is not None and (type(value) is not int or not 0 <= value <= limit):
                raise ValueError("invalid_token_detail")


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    call_number: int
    stage: ModelCallStage
    outcome: ModelCallOutcome
    usage_status: UsageStatus
    tokens: TokenUsage | None

    def __post_init__(self) -> None:
        if type(self.call_number) is not int or self.call_number < 1:
            raise ValueError("invalid_call_number")
        if not (
            isinstance(self.stage, ModelCallStage)
            and isinstance(self.outcome, ModelCallOutcome)
            and isinstance(self.usage_status, UsageStatus)
        ):
            raise ValueError("invalid_call_usage")
        if self.outcome is not ModelCallOutcome.RETURNED and self.usage_status is not UsageStatus.MISSING:
            raise ValueError("invalid_call_usage")
        if self.usage_status is UsageStatus.REPORTED:
            if not isinstance(self.tokens, TokenUsage):
                raise ValueError("invalid_call_usage")
        elif self.tokens is not None:
            raise ValueError("invalid_call_usage")


@dataclass(frozen=True, slots=True)
class AgentUsageSnapshot:
    run_id: UUID
    calls: tuple[ModelCallUsage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not isinstance(self.calls, tuple) or any(
            not isinstance(call, ModelCallUsage) or call.call_number != number
            for number, call in enumerate(self.calls, 1)
        ):
            raise ValueError("invalid_usage_snapshot")

    @property
    def model_calls_started(self) -> int:
        return len(self.calls)

    @property
    def reported_calls(self) -> int:
        return sum(call.usage_status is UsageStatus.REPORTED for call in self.calls)

    @property
    def unknown_usage_calls(self) -> int:
        return self.model_calls_started - self.reported_calls

    @property
    def known_input_tokens(self) -> int:
        return sum(call.tokens.input_tokens for call in self.calls if call.tokens is not None)

    @property
    def known_output_tokens(self) -> int:
        return sum(call.tokens.output_tokens for call in self.calls if call.tokens is not None)

    @property
    def known_total_tokens(self) -> int:
        return sum(call.tokens.total_tokens for call in self.calls if call.tokens is not None)

    @property
    def usage_complete(self) -> bool:
        """Whether every observed call reported usage, including the empty scope."""
        return self.unknown_usage_calls == 0


class UsageCollector:
    def __init__(self, run_id: UUID) -> None:
        if not isinstance(run_id, UUID):
            raise ValueError("invalid_usage_run_id")
        self._run_id = run_id
        self._calls: list[ModelCallUsage] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start_call(self, stage: ModelCallStage) -> int:
        self._require_open()
        number = len(self._calls) + 1
        self._calls.append(ModelCallUsage(number, stage, ModelCallOutcome.STARTED, UsageStatus.MISSING, None))
        return number

    def finish_call(
        self, call_number: int, *, outcome: ModelCallOutcome, usage_status: UsageStatus, tokens: TokenUsage | None,
    ) -> None:
        self._require_open()
        if type(call_number) is not int or not 1 <= call_number <= len(self._calls):
            raise ValueError("unknown_model_call")
        index = call_number - 1
        call = self._calls[index]
        if call.outcome is not ModelCallOutcome.STARTED:
            raise RuntimeError("model_call_already_finished")
        if outcome is ModelCallOutcome.STARTED:
            raise ValueError("invalid_call_outcome")
        self._calls[index] = ModelCallUsage(call_number, call.stage, outcome, usage_status, tokens)

    def snapshot(self) -> AgentUsageSnapshot:
        return AgentUsageSnapshot(self._run_id, tuple(self._calls))

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("usage_collector_closed")


_current_usage: ContextVar[UsageCollector | None] = ContextVar("current_agent_usage", default=None)


@contextmanager
def capture_agent_usage(run_id: UUID) -> Iterator[UsageCollector]:
    """Capture the model calls for one explicitly scoped AgentRun execution."""
    collector = UsageCollector(run_id)
    token = _current_usage.set(collector)
    try:
        yield collector
    finally:
        collector.close()
        _current_usage.reset(token)


def current_usage_collector() -> UsageCollector | None:
    collector = _current_usage.get()
    return collector if collector is not None and not collector.closed else None
