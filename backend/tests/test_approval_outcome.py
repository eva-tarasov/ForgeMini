import asyncio
from dataclasses import fields, replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services.approval_outcome import ApprovalOutcome, ApprovalOutcomeHolder, ApprovalOutcomePort
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.services.tool_call_audit import ToolCallErrorCode
from app.tools.errors import ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from test_approval_contracts import make_operation


@pytest.fixture
def operation(tmp_path):
    return asyncio.run(_make_operation(tmp_path))


async def _make_operation(root):
    return make_operation(root)


def test_holder_binds_exact_operation_and_accepts_publication_once(operation):
    holder = ApprovalOutcomeHolder(operation.reference.ticket.run_id)
    port = holder.port()
    outcome = ApprovalOutcome(operation.reference, ToolCallErrorCode.APPROVAL_REJECTED)

    assert not port.publish(outcome)
    assert port.bind(operation.reference)
    assert port.bind(replace(operation.reference))
    assert port.publish(outcome)
    assert port.publish(replace(outcome))
    assert holder.take_and_close() is outcome
    assert holder.take_and_close() is None
    assert not port.bind(operation.reference)
    assert not port.publish(outcome)


@pytest.mark.parametrize("change", [
    {"code": ToolCallErrorCode.CANCELLED},
    {"interrupted_request": True},
], ids=["different-code", "different-interruption"])
def test_holder_invalidates_conflicting_publications(operation, change):
    holder = ApprovalOutcomeHolder(operation.reference.ticket.run_id)
    port = holder.port()
    outcome = ApprovalOutcome(operation.reference, ToolCallErrorCode.APPROVAL_REJECTED)

    assert port.bind(operation.reference)
    assert port.publish(outcome)
    assert not port.publish(replace(outcome, **change))
    assert not port.publish(outcome)
    assert holder.take_and_close() is None


@pytest.mark.parametrize("wrong", ["run", "approval", "ticket", "workspace", "patch", "sources"])
def test_holder_rejects_foreign_reference_without_rebinding(operation, wrong):
    reference = operation.reference
    if wrong == "run":
        foreign = replace(reference, ticket=replace(reference.ticket, run_id=uuid4()))
    elif wrong == "approval":
        foreign = replace(reference, approval_id=uuid4())
    elif wrong == "ticket":
        foreign = replace(reference, ticket=replace(reference.ticket, tool_call_id=uuid4()))
    elif wrong == "workspace":
        foreign = replace(reference, ticket=replace(reference.ticket, workspace_id=uuid4()))
    elif wrong == "patch":
        foreign = replace(reference, patch_sha256="a" * 64)
    else:
        foreign = replace(reference, sources_sha256="b" * 64)

    holder = ApprovalOutcomeHolder(reference.ticket.run_id)
    port = holder.port()
    if wrong == "run":
        assert not port.bind(foreign)
    outcome = ApprovalOutcome(reference, ToolCallErrorCode.APPROVAL_REJECTED)
    assert port.bind(reference)
    assert port.publish(outcome)
    assert not port.bind(foreign)
    assert not port.publish(ApprovalOutcome(foreign, ToolCallErrorCode.CANCELLED))
    assert holder.take_and_close() is outcome


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_holder_closes_without_publication(operation, bound):
    holder = ApprovalOutcomeHolder(operation.reference.ticket.run_id)
    port = holder.port()

    if bound:
        assert port.bind(operation.reference)
    assert holder.take_and_close() is None
    assert not port.bind(operation.reference)
    assert not port.publish(ApprovalOutcome(operation.reference, ToolCallErrorCode.CANCELLED))


def test_holders_isolate_two_invocations_even_for_same_run(operation):
    first_reference = operation.reference
    second_reference = replace(first_reference, approval_id=uuid4())
    first, second = (ApprovalOutcomeHolder(first_reference.ticket.run_id) for _ in range(2))
    first_port, second_port = first.port(), second.port()
    first_outcome = ApprovalOutcome(first_reference, ToolCallErrorCode.CANCELLED, interrupted_request=True)
    second_outcome = ApprovalOutcome(second_reference, ToolCallErrorCode.APPROVAL_REJECTED)

    assert first_port.bind(first_reference)
    assert second_port.bind(second_reference)
    assert not first_port.publish(second_outcome)
    assert not second_port.publish(first_outcome)
    assert first_port.publish(first_outcome)
    assert second_port.publish(second_outcome)
    assert first.take_and_close() is first_outcome
    assert not first_port.publish(first_outcome)
    assert second.take_and_close() is second_outcome


def test_holder_keeps_only_metadata_and_diagnostics_hide_reference(operation):
    holder = ApprovalOutcomeHolder(operation.reference.ticket.run_id)
    port = holder.port()
    outcome = ApprovalOutcome(operation.reference, ToolCallErrorCode.CANCELLED, interrupted_request=True)
    assert port.bind(operation.reference)
    assert port.publish(outcome)

    assert {field.name for field in fields(outcome)} == {"reference", "code", "interrupted_request"}
    diagnostics = repr((holder, port, outcome))
    for private in (operation.patch, "fixture.txt", "untouched context", operation.reference.patch_sha256):
        assert private not in diagnostics
    assert holder.take_and_close() is outcome


@pytest.mark.parametrize("reason", ["closed", "other-operation", "other-run"])
def test_executor_rejects_unbound_approval_outcome_before_request(operation, monkeypatch, reason):
    run_id = uuid4() if reason == "other-run" else operation.context.run_id
    holder = ApprovalOutcomeHolder(run_id)
    port = holder.port()
    if reason == "closed":
        assert holder.take_and_close() is None
    elif reason == "other-operation":
        assert port.bind(operation.reference)

    session_factory = Mock()
    executor = ApprovedToolExecutor(ToolExecutor(ToolRegistry([operation.tool])), session_factory, outcome=port)
    request = AsyncMock(side_effect=AssertionError("ApprovalGate.request вызван после отказа bind()"))
    handler = AsyncMock()
    reserve = Mock(wraps=executor._pending.reserve)
    discard = Mock(wraps=executor._pending.discard)
    publish = Mock(wraps=executor._pending.publish)
    monkeypatch.setattr(executor._gate, "request", request)
    monkeypatch.setattr(executor._executor, "_invoke_handler", handler)
    monkeypatch.setattr(executor._pending, "reserve", reserve)
    monkeypatch.setattr(executor._pending, "discard", discard)
    monkeypatch.setattr(executor._pending, "publish", publish)

    async def check():
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            await executor.execute("apply_patch", {"patch": operation.patch}, context=operation.context)
        assert executor.pending_request(operation.context) is None
        assert not executor._pending._entries

    asyncio.run(check())
    reserve.assert_called_once()
    (proposal,) = reserve.call_args.args
    discard.assert_called_once_with(proposal)
    request.assert_not_awaited()
    handler.assert_not_awaited()
    publish.assert_not_called()
    session_factory.assert_not_called()


@pytest.mark.parametrize("publisher_error", [RuntimeError, asyncio.CancelledError])
def test_publication_failure_preserves_original_cancellation_and_cleanup(operation, monkeypatch, publisher_error):
    captured = []
    holder = ApprovalOutcomeHolder(operation.context.run_id)
    port = holder.port()
    original_error = asyncio.CancelledError("original cancellation")

    def fail_to_publish(outcome):
        assert port.publish(outcome)
        captured.append(outcome)
        raise publisher_error("sensitive synthetic diagnostic")

    executor = ApprovedToolExecutor(
        ToolExecutor(ToolRegistry([operation.tool])), None,
        outcome=ApprovalOutcomePort(port.bind, fail_to_publish),
    )
    request = AsyncMock(side_effect=original_error)
    cleanup = AsyncMock()
    monkeypatch.setattr(executor._gate, "request", request)
    monkeypatch.setattr(executor._gate, "_reconcile_unpublished_request", cleanup)

    async def check():
        with pytest.raises(asyncio.CancelledError) as caught:
            await executor.execute("apply_patch", {"patch": operation.patch}, context=operation.context)
        assert caught.value is original_error
        assert caught.value.__notes__ == ["approval_outcome_publication_failed"]
        assert not executor._pending._entries

    asyncio.run(check())
    (proposal,) = request.await_args.args
    cleanup.assert_awaited_once_with(proposal, ToolCallErrorCode.CANCELLED)
    assert captured == [ApprovalOutcome(proposal.reference, ToolCallErrorCode.CANCELLED, interrupted_request=True)]
