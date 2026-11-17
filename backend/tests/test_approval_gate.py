import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope
from app.services.approval_contracts import ApprovalDecision
from app.services.approved_tool_executor import ApprovedToolExecutor
from app.tools.errors import ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository.factory import create_fixture_patch_tool
from test_approval_contracts import make_operation, patch_for, scoped_context


@pytest.mark.parametrize("missing", ["read-capability", "write-capability", "read-scope", "write-scope"])
def test_missing_grant_cannot_open_approval_transaction(tmp_path, missing, monkeypatch):
    context = scoped_context()
    if missing == "read-capability":
        context = replace(context, capabilities=frozenset({Capability.REPOSITORY_WRITE}))
    elif missing == "write-capability":
        context = replace(context, capabilities=frozenset({Capability.REPOSITORY_READ}))
    elif missing == "read-scope":
        context = replace(context, read_scopes=(PathScope("a.txt"),))
    else:
        context = replace(context, write_scopes=(PathScope("a.txt"),))
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    factory = Mock()
    executor = ApprovedToolExecutor(ToolExecutor(ToolRegistry([tool])), factory)
    monkeypatch.setattr(type(tool.handler.keywords["writer"]), "preview", Mock(side_effect=AssertionError("read")))
    with pytest.raises(ToolExecutionError, match="^permission_denied$"):
        asyncio.run(executor.execute("apply_patch", {"patch": patch_for("a.txt", "b.txt")}, context=context))
    factory.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_gate_rejects_fake_operation_identifier_and_untyped_decision_before_database(tmp_path):
    async def check():
        op = make_operation(tmp_path)
        factory = Mock()
        approved = ApprovedToolExecutor(ToolExecutor(ToolRegistry([op.tool])), factory)
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            await approved.decide(op.reference, ApprovalDecision.APPROVE, context=op.context)
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            await approved._gate.claim(op)
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            await approved._gate.claim(object())
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            await approved.decide(op.reference, "approve", context=op.context)
        factory.assert_not_called()

    asyncio.run(check())


def test_approved_route_rejects_fabricated_handler_and_model_approval_flag(tmp_path):
    context = scoped_context()
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    factory = Mock()

    async def arbitrary(_arguments):
        pytest.fail("An arbitrary handler must not acquire approval authority")

    fake = ApprovedToolExecutor(ToolExecutor(ToolRegistry([replace(tool, handler=arbitrary)])), factory)
    with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
        asyncio.run(fake.execute("apply_patch", {"patch": patch_for("fixture.txt")}, context=context))
    exact = ApprovedToolExecutor(ToolExecutor(ToolRegistry([tool])), factory)
    with pytest.raises(ToolExecutionError, match="^invalid_input$"):
        asyncio.run(exact.execute("apply_patch", {"patch": patch_for("fixture.txt"), "approved": True}, context=context))
    factory.assert_not_called()
