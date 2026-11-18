from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.services.approval_contracts import ApprovalReference
from app.services.tool_call_audit import ToolCallErrorCode


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """Подсказка для поиска outcome в PostgreSQL."""

    reference: ApprovalReference = field(repr=False)
    code: ToolCallErrorCode
    interrupted_request: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalOutcomePort:
    bind: Callable[[ApprovalReference], bool] = field(repr=False)
    publish: Callable[[ApprovalOutcome], bool] = field(repr=False)


class ApprovalOutcomeHolder:
    """Метаданные одной invocation. Привязка и публикация выполняются в том же event loop."""

    __slots__ = ("_run_id", "_reference", "_outcome", "_conflicted", "_closed")

    def __init__(self, run_id: UUID) -> None:
        self._run_id = run_id
        self._reference: ApprovalReference | None = None
        self._outcome: ApprovalOutcome | None = None
        self._conflicted = False
        self._closed = False

    def port(self) -> ApprovalOutcomePort:
        return ApprovalOutcomePort(self._bind, self._publish)

    def _bind(self, reference: ApprovalReference) -> bool:
        if (
            self._closed or self._conflicted or type(reference) is not ApprovalReference
            or reference.ticket.run_id != self._run_id
        ):
            return False
        if self._reference is None:
            self._reference = reference
        return self._reference == reference

    def _publish(self, outcome: ApprovalOutcome) -> bool:
        if (
            self._closed or self._conflicted or self._reference is None
            or type(outcome) is not ApprovalOutcome or type(outcome.reference) is not ApprovalReference
            or outcome.reference != self._reference or type(outcome.code) is not ToolCallErrorCode
            or type(outcome.interrupted_request) is not bool
        ):
            return False
        if self._outcome is None:
            self._outcome = outcome
        elif self._outcome != outcome:
            self._outcome = None
            self._conflicted = True
            return False
        return True

    def take_and_close(self) -> ApprovalOutcome | None:
        outcome = self._outcome
        self._closed = True
        self._reference = None
        self._outcome = None
        return outcome
