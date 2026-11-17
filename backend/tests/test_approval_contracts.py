import asyncio
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from uuid import uuid4

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.services.approval_contracts import PendingOperation, patch_fingerprint, validate_patch_proposal
from app.tools.errors import ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository.factory import create_fixture_patch_tool
from app.tools.repository.writer import PatchApplyError


def patch_for(*names: str, after: str = "after") -> str:
    return "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1,1 +1,1 @@\n-before\n+{after}\n" for name in names)


def scoped_context() -> PermissionContext:
    scope = (PathScope(".", recursive=True),)
    return PermissionContext(uuid4(), uuid4(), frozenset(Capability), read_scopes=scope, write_scopes=scope)


def make_operation(root, *, context=None):
    context = scoped_context() if context is None else context
    (root / "fixture.txt").write_bytes(b"before\nuntouched context\n")
    executor = ToolExecutor(ToolRegistry([create_fixture_patch_tool(root=root, context=context)]))
    tool, typed, writer = validate_patch_proposal(
        executor, "apply_patch", {"patch": patch_for("fixture.txt")}, version=1, context=context,
    )
    return PendingOperation.create(
        context, tool, writer, typed, writer.preview(typed.patch), asyncio.get_running_loop().time() + 10,
    )


def test_approval_binds_exact_patch_bytes_and_metadata_only_reference(tmp_path):
    async def check():
        operation = make_operation(tmp_path)
        operation.check_identity()
        text = operation.patch.encode()
        assert operation.reference.patch_sha256 == sha256(b"forge:apply_patch:v1\0" + text).hexdigest()
        assert operation.reference.patch_sha256 != sha256(text).hexdigest()
        payload = operation.reference.requested_payload()
        assert set(payload) == {
            "schema_version", "approval_id", "tool_call_id", "workspace_id", "tool_name", "tool_version",
            "patch_sha256", "sources_sha256",
        }
        assert "fixture.txt" not in repr(operation) and "untouched context" not in repr(operation)
        with pytest.raises(FrozenInstanceError):
            operation.patch = "another patch"
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            replace(operation, patch=patch_for("fixture.txt", after="other")).check_identity()
        with pytest.raises(ToolExecutionError, match="^approval_invalid$"):
            replace(operation, sources=(replace(operation.sources[0], mode=0o777),)).check_identity()
        with pytest.raises(ValueError, match="^invalid_approval_reference$"):
            replace(operation.reference, ticket=replace(operation.reference.ticket, tool_version=True))

    asyncio.run(check())


def test_approval_preview_and_apply_fingerprint_cover_bytes_outside_hunk(tmp_path, monkeypatch):
    async def check():
        operation = make_operation(tmp_path)
        file = tmp_path / "fixture.txt"
        assert file.read_bytes() == b"before\nuntouched context\n"
        assert list(tmp_path.iterdir()) == [file]
        file.write_bytes(b"before\nchanged outside hunk\n")
        assert operation.writer.preview(operation.patch) != operation.sources

        def no_stage(*_args, **_kwargs):
            pytest.fail("Snapshot mismatch must be detected before staging")

        monkeypatch.setattr(type(operation.writer), "_stage", no_stage)
        with pytest.raises(PatchApplyError, match="^patch_conflict$"):
            operation.writer.apply(operation.patch, expected_sources=operation.sources)
        assert file.read_bytes() == b"before\nchanged outside hunk\n"

    asyncio.run(check())
