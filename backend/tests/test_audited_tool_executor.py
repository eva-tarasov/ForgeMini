import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.domain.capabilities import Capability
from app.domain.execution import ToolCallStatus as Status
from app.permissions.context import PermissionContext
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_call_audit import ToolCallAuditError, ToolCallErrorCode as Code
from app.services.tool_calls import ToolCallTicket
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import RegisteredTool, ToolRegistry


class SampleInput(ToolInput):
    label: str


class SampleOutput(ToolOutput):
    count: int


CONTEXT = PermissionContext(UUID(int=1), UUID(int=2), frozenset({Capability.REPOSITORY_READ}))
SPEC = ToolSpec(
    name="read_fixture", version=2, description="Synthetic audit test", input_schema=SampleInput,
    output_schema=SampleOutput, capability=Capability.REPOSITORY_READ, risk_level=ToolRiskLevel.LOW,
    timeout_ms=1000, side_effect=False, requires_approval=False,
)
TICKET = ToolCallTicket(UUID(int=3), CONTEXT.run_id, CONTEXT.workspace_id, SPEC.name, SPEC.version)


def make_executor(handler, spec=SPEC):
    return ToolExecutor(ToolRegistry([RegisteredTool(spec, handler, synthetic=True)]))


def make_service():
    return SimpleNamespace(propose=AsyncMock(return_value=TICKET), start=AsyncMock(), finish=AsyncMock())


@pytest.mark.parametrize(
    ("name", "arguments", "version", "context", "expected"),
    [
        ("synthetic-private-name", {}, None, CONTEXT, ToolErrorCode.UNKNOWN_TOOL),
        ("", {}, None, CONTEXT, ToolErrorCode.INVALID_REQUEST),
        (SPEC.name, [], None, CONTEXT, ToolErrorCode.INVALID_REQUEST),
        (SPEC.name, {}, True, CONTEXT, ToolErrorCode.INVALID_REQUEST),
        (SPEC.name, {"label": "fixture"}, None, None, ToolErrorCode.PERMISSION_DENIED),
        (SPEC.name, {"label": "fixture"}, None, {}, ToolErrorCode.PERMISSION_DENIED),
    ],
)
def test_wrapper_does_not_persist_invalid_envelopes(name, arguments, version, context, expected):
    handler, service = AsyncMock(), make_service()
    wrapper = AuditedToolExecutor(make_executor(handler), service)
    with pytest.raises(ToolExecutionError) as caught:
        asyncio.run(wrapper.execute(name, arguments, context=context, version=version))
    assert caught.value.code is expected
    service.propose.assert_not_awaited()
    service.start.assert_not_awaited()
    service.finish.assert_not_awaited()
    handler.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "version", "context", "approval", "status", "code"),
    [
        ({"label": 1}, None, CONTEXT, False, Status.FAILED, Code.INVALID_INPUT),
        ({"label": "fixture"}, 50, CONTEXT, False, Status.FAILED, Code.VERSION_MISMATCH),
        ({"label": "fixture"}, None, replace(CONTEXT, capabilities=frozenset()), False,
         Status.DENIED, Code.PERMISSION_DENIED),
        ({"label": "fixture"}, None, CONTEXT, True, Status.DENIED, Code.EXECUTION_BLOCKED),
    ],
)
def test_wrapper_maps_prestart_failures(arguments, version, context, approval, status, code):
    handler, service = AsyncMock(), make_service()
    spec = SPEC.model_copy(update={"requires_approval": approval})
    wrapper = AuditedToolExecutor(make_executor(handler, spec), service)
    with pytest.raises(ToolExecutionError) as caught:
        asyncio.run(wrapper.execute(spec.name, arguments, context=context, version=version))
    assert caught.value.code.value == code.value
    service.propose.assert_awaited_once_with(context, spec)
    service.start.assert_not_awaited()
    service.finish.assert_awaited_once_with(TICKET, status, error_code=code, duration_ms=None)
    handler.assert_not_called()


@pytest.mark.parametrize("phase", ["propose", "start"])
def test_wrapper_halts_before_handler_when_admission_persistence_fails(phase):
    handler, service = AsyncMock(), make_service()
    original = ToolCallAuditError(Code.AUDIT_PERSISTENCE_FAILED)
    getattr(service, phase).side_effect = original
    wrapper = AuditedToolExecutor(make_executor(handler), service)
    with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$") as caught:
        asyncio.run(wrapper.execute(SPEC.name, {"label": "fixture"}, context=CONTEXT))
    assert caught.value is original
    handler.assert_not_called()
    if phase == "propose":
        service.start.assert_not_awaited()
        service.finish.assert_not_awaited()
    else:
        service.finish.assert_awaited_once_with(
            TICKET, Status.FAILED, error_code=Code.AUDIT_PERSISTENCE_FAILED, duration_ms=None,
        )


def test_wrapper_does_not_return_success_or_retry_after_final_audit_failure():
    handler, service = AsyncMock(return_value=SampleOutput(count=1)), make_service()
    service.finish.side_effect = ToolCallAuditError(Code.AUDIT_PERSISTENCE_FAILED)
    wrapper = AuditedToolExecutor(make_executor(handler), service)
    with pytest.raises(ToolCallAuditError, match="^audit_persistence_failed$"):
        asyncio.run(wrapper.execute(SPEC.name, {"label": "fixture"}, context=CONTEXT))
    handler.assert_awaited_once_with(SampleInput(label="fixture"))
    assert service.finish.await_args.args == (TICKET, Status.SUCCEEDED)
    assert service.finish.await_args.kwargs["duration_ms"] >= 0
    service.finish.assert_awaited_once()


@pytest.mark.parametrize("cancelled", [False, True])
def test_wrapper_preserves_primary_error_and_adds_safe_audit_note(monkeypatch, cancelled):
    handler, service = AsyncMock(), make_service()
    primary = asyncio.CancelledError() if cancelled else ToolExecutionError(ToolErrorCode.INVALID_OUTPUT)
    service.finish.side_effect = RuntimeError("synthetic-db-secret")
    executor = make_executor(handler)
    monkeypatch.setattr(executor, "execute", AsyncMock(side_effect=primary))
    wrapper = AuditedToolExecutor(executor, service)

    with pytest.raises(type(primary)) as caught:
        asyncio.run(wrapper.execute(SPEC.name, {"label": "fixture"}, context=CONTEXT))
    assert caught.value is primary
    assert primary.__notes__ == ["tool_call_audit_finalization_failed"]
    service.finish.assert_awaited_once_with(
        TICKET, Status.FAILED, error_code=Code.CANCELLED if cancelled else Code.INVALID_OUTPUT, duration_ms=None,
    )


def test_wrapper_propagates_cancellation_during_success_finalization():
    handler, service = AsyncMock(return_value=SampleOutput(count=1)), make_service()
    original = asyncio.CancelledError()
    service.finish.side_effect = original
    wrapper = AuditedToolExecutor(make_executor(handler), service)

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(wrapper.execute(SPEC.name, {"label": "fixture"}, context=CONTEXT))
    assert caught.value is original
    handler.assert_awaited_once()
    service.finish.assert_awaited_once()
