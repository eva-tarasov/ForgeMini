import asyncio
from dataclasses import replace

import pytest

from app.services.approval_contracts import ApprovalDecision
from app.services.pending_approvals import PendingApprovals
from app.tools.errors import ToolExecutionError
from test_approval_contracts import make_operation


def test_registry_hides_uncommitted_slots_and_requires_exact_context_and_operation(tmp_path):
    async def check():
        registry = PendingApprovals()
        operation = make_operation(tmp_path)
        registry.reserve(operation)
        assert registry.request_for(operation.context) is None
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            registry.resolve(operation.reference, operation.context)
        registry.publish(operation)
        assert registry.request_for(operation.context) is operation.reference
        assert registry.resolve(operation.reference, operation.context) is operation
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            registry.resolve(operation.reference, replace(operation.context))
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            registry.require_published(replace(operation))
        registry.discard(operation)
        assert registry.request_for(operation.context) is None

    asyncio.run(check())


def test_registry_enforces_capacity_and_one_active_wait_per_run(tmp_path):
    async def check():
        first = make_operation(tmp_path)
        same_run = make_operation(tmp_path, context=first.context)
        other_run = make_operation(tmp_path)
        registry = PendingApprovals(capacity=2)
        registry.reserve(first)
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            registry.reserve(same_run)
        registry.reserve(other_run)
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            registry.reserve(make_operation(tmp_path))
        registry.discard(first)
        registry.reserve(same_run)

    asyncio.run(check())


def test_registry_notifies_once_and_waits_until_monotonic_deadline(tmp_path):
    async def check():
        registry = PendingApprovals()
        expired = replace(make_operation(tmp_path), deadline=asyncio.get_running_loop().time())
        registry.reserve(expired)
        registry.publish(expired)
        with pytest.raises(TimeoutError):
            await registry.wait(expired)
        registry.discard(expired)
        operation = make_operation(tmp_path)
        registry.reserve(operation)
        registry.publish(operation)
        registry.notify(operation, ApprovalDecision.APPROVE)
        registry.notify(operation, ApprovalDecision.REJECT)
        assert await registry.wait(operation) is ApprovalDecision.APPROVE
        registry.discard(operation)

    asyncio.run(check())
