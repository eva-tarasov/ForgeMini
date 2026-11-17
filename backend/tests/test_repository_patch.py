import asyncio
import os
import stat
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.tools.contracts import ToolRiskLevel
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository import filesystem, writer
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor, create_fixture_patch_tool
from app.tools.repository.patch import PATCH_TEMP_PREFIX
from app.tools.repository.schemas import ApplyPatchInput, ApplyPatchOutput
from app.tools.repository.writer import PatchApplyError, SafeRepositoryWriter


def context_for(*, read=None, write=None, capabilities=None):
    root_scope = (PathScope(".", recursive=True),)
    return PermissionContext(
        run_id=uuid4(),
        workspace_id=uuid4(),
        capabilities=frozenset(Capability) if capabilities is None else frozenset(capabilities),
        read_scopes=root_scope if read is None else read,
        write_scopes=root_scope if write is None else write,
    )


def patch_for(*paths):
    return "".join(f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-before\n+after\n" for path in paths)


def direct(tool, patch):
    return asyncio.run(tool.handler(ApplyPatchInput(patch=patch)))


def staging_names(root):
    return sorted(path.name for path in root.rglob(f"{PATCH_TEMP_PREFIX}*"))


def test_patch_factory_binds_write_spec_and_keeps_read_factory_separate(tmp_path):
    context = context_for()
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    registry = ToolRegistry([tool])
    registered = registry.get("apply_patch", version=1)

    assert registered.bound_context is context and registered.synthetic is False
    assert registered.spec.capability is Capability.REPOSITORY_WRITE
    assert registered.spec.risk_level is ToolRiskLevel.HIGH
    assert registered.spec.side_effect and registered.spec.requires_approval
    assert registered.spec.audit_policy == "metadata_only" and registered.spec.timeout_ms == 2000
    arguments = ApplyPatchInput(patch=patch_for("z.txt", "src/a.txt"))
    assert registered.path_selector(arguments) == ("z.txt", "src/a.txt")
    read_only = create_fixture_executor(root=tmp_path, context=context, baseline=FixtureBaseline("test", {}))
    assert {spec.name for spec in read_only._registry.list_specs()} == {
        "read_file", "list_files", "search_files", "git_diff",
    }
    with pytest.raises(ToolExecutionError, match="^unknown_tool$"):
        read_only.get_tool_spec("apply_patch")


def test_fixture_patch_replaces_single_file_and_preserves_mode(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"before\nuntouched trailing\n")
    target.chmod(0o640)
    original_id = target.stat().st_ino
    (tmp_path / "unrelated").write_bytes(b"unrelated\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())

    assert direct(tool, patch_for("a.txt")) == ApplyPatchOutput(changed_paths=("a.txt",), changed_files=1)
    assert target.read_bytes() == b"after\nuntouched trailing\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert target.stat().st_ino != original_id
    assert (tmp_path / "unrelated").read_bytes() == b"unrelated\n"
    assert staging_names(tmp_path) == []


def test_fixture_patch_stages_all_files_before_ordered_replacement(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    for path in ("b.txt", "src/a.txt"):
        (tmp_path / path).write_bytes(b"before\n")
    context = context_for(
        read=(PathScope("b.txt"), PathScope("src/a.txt")), write=(PathScope("b.txt"), PathScope("src/a.txt")),
    )
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    replaced = []
    original_replace = os.replace

    def observed_replace(source, target, *, src_dir_fd, dst_dir_fd):
        assert src_dir_fd == dst_dir_fd
        if not replaced:
            assert len(staging_names(tmp_path)) == 2
            assert (tmp_path / "b.txt").read_bytes() == (tmp_path / "src/a.txt").read_bytes() == b"before\n"
        result = original_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        replaced.append(target)
        return result

    monkeypatch.setattr(os, "replace", observed_replace)
    output = direct(tool, patch_for("b.txt", "src/a.txt"))

    assert output.changed_paths == ("b.txt", "src/a.txt") and output.changed_files == 2
    assert replaced == ["b.txt", "a.txt"]
    assert (tmp_path / "b.txt").read_bytes() == (tmp_path / "src/a.txt").read_bytes() == b"after\n"
    assert staging_names(tmp_path) == []


@pytest.mark.parametrize("missing", ["read-capability", "write-capability", "read-scope", "write-scope"])
def test_whole_operation_denial_never_accesses_first_or_second_target(tmp_path, monkeypatch, missing):
    for name in ("first.txt", "second.txt"):
        (tmp_path / name).write_bytes(b"before\n")
    if missing == "read-capability":
        context = context_for(capabilities={Capability.REPOSITORY_WRITE})
    elif missing == "write-capability":
        context = context_for(capabilities={Capability.REPOSITORY_READ})
    elif missing == "read-scope":
        context = context_for(read=(PathScope("first.txt"),))
    else:
        context = context_for(write=(PathScope("first.txt"),))
    tool = create_fixture_patch_tool(root=tmp_path, context=context)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Denied patch must not inspect targets or create staging files")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "stat", forbidden)
        patch.setattr(os, "replace", forbidden)
        with pytest.raises(PatchApplyError, match="^permission_denied$"):
            direct(tool, patch_for("first.txt", "second.txt"))
    assert (tmp_path / "first.txt").read_bytes() == (tmp_path / "second.txt").read_bytes() == b"before\n"
    assert staging_names(tmp_path) == []


@pytest.mark.parametrize("path", [".Git/config", ".env.local", "../outside.txt", "/absolute.txt"])
def test_broad_grants_cannot_accept_protected_or_malformed_paths(tmp_path, path):
    context = context_for()
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    executor = ToolExecutor(ToolRegistry([tool]))
    with pytest.raises(ToolExecutionError, match="^invalid_input$"):
        asyncio.run(executor.execute("apply_patch", {"patch": patch_for(path)}, context=context))
    assert list(tmp_path.iterdir()) == []


def test_fixture_patch_prepares_all_targets_before_any_staging(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"before\n")
    (tmp_path / "b.txt").write_bytes(b"stale\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())

    def forbidden(*_args, **_kwargs):
        pytest.fail("A stale second target must fail before staging the first")

    monkeypatch.setattr(SafeRepositoryWriter, "_stage", forbidden)
    with pytest.raises(PatchApplyError, match="^patch_conflict$"):
        direct(tool, patch_for("a.txt", "b.txt"))
    assert (tmp_path / "a.txt").read_bytes() == b"before\n"
    assert (tmp_path / "b.txt").read_bytes() == b"stale\n"


def test_fixture_patch_refuses_symlinks_hard_links_and_nonregular_files(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "another-fixture"
    root.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"before\n")
    (root / "target.txt").write_bytes(b"before\n")
    (root / "intermediate").symlink_to(outside, target_is_directory=True)
    (root / "outside-link").symlink_to(outside / "sentinel.txt")
    (root / "inside-link").symlink_to(root / "target.txt")
    os.link(outside / "sentinel.txt", root / "hard-link")
    os.mkfifo(root / "fifo")
    (root / "directory").mkdir()
    tool = create_fixture_patch_tool(root=root, context=context_for())
    for path in ("intermediate/sentinel.txt", "outside-link", "inside-link", "hard-link", "fifo", "directory"):
        with pytest.raises(PatchApplyError):
            direct(tool, patch_for(path))
    assert (outside / "sentinel.txt").read_bytes() == (root / "target.txt").read_bytes() == b"before\n"
    assert staging_names(root) == []


@pytest.mark.parametrize("mode", [0o440, 0o1640], ids=["read-only", "sticky"])
def test_fixture_patch_refuses_read_only_or_special_mode(tmp_path, mode):
    file = tmp_path / "target.txt"
    file.write_bytes(b"before\n")
    file.chmod(mode)
    assert stat.S_IMODE(file.stat().st_mode) == mode
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    with pytest.raises(PatchApplyError, match="^patch_unsupported_target$"):
        direct(tool, patch_for("target.txt"))
    assert file.read_bytes() == b"before\n"
    assert stat.S_IMODE(file.stat().st_mode) == mode
    assert staging_names(tmp_path) == []


def test_fixture_patch_rejects_replaced_root_and_case_mismatch(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "Exact.txt").write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=root, context=context_for())
    with pytest.raises(PatchApplyError, match="^patch_io_failed$"):
        direct(tool, patch_for("exact.txt"))
    root.rename(tmp_path / "old")
    root.mkdir()
    (root / "Exact.txt").write_bytes(b"before\n")
    with pytest.raises(PatchApplyError, match="^patch_io_failed$"):
        direct(tool, patch_for("Exact.txt"))
    assert (root / "Exact.txt").read_bytes() == (tmp_path / "old" / "Exact.txt").read_bytes() == b"before\n"


def test_fixture_patch_refuses_wrong_device(tmp_path, monkeypatch):
    file = tmp_path / "file.txt"
    file.write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    original_stat = os.stat

    def different_device(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == "file.txt":
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "stat", different_device)
    with pytest.raises(PatchApplyError, match="^patch_unsupported_target$"):
        direct(tool, patch_for("file.txt"))
    assert file.read_bytes() == b"before\n"


def test_fixture_patch_fails_closed_without_descriptor_rename_primitives(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.rename})
    with pytest.raises(PatchApplyError, match="^patch_primitives_unavailable$"):
        create_fixture_patch_tool(root=tmp_path, context=context_for())


@pytest.mark.parametrize("data", [b"before\r\n", b"before", b"\xff\n", b"before\0\n"])
def test_fixture_patch_refuses_unsupported_source_text(tmp_path, data):
    file = tmp_path / "file.txt"
    file.write_bytes(data)
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    with pytest.raises(PatchApplyError, match="^patch_unsupported_text$"):
        direct(tool, patch_for("file.txt"))
    assert file.read_bytes() == data and staging_names(tmp_path) == []


def test_fixture_patch_rejects_oversized_result(tmp_path):
    data = b"before\n" + b"x" * 65528 + b"\n"
    (tmp_path / "file.txt").write_bytes(data)
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    patch = patch_for("file.txt").replace("+after\n", "+longer-text\n")
    with pytest.raises(PatchApplyError, match="^patch_limit_exceeded$"):
        direct(tool, patch)
    assert (tmp_path / "file.txt").read_bytes() == data and staging_names(tmp_path) == []


def test_fixture_patch_shares_directory_budget_across_revalidation(tmp_path, monkeypatch):
    (tmp_path / "file.txt").write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    monkeypatch.setattr(filesystem, "MAX_EXAMINED_ENTRIES", 3)
    with pytest.raises(PatchApplyError, match="^patch_conflict$"):
        direct(tool, patch_for("file.txt"))
    assert (tmp_path / "file.txt").read_bytes() == b"before\n" and staging_names(tmp_path) == []


@pytest.mark.parametrize("replacement", ["bytes", "inode"], ids=["changed-bytes", "new-inode"])
def test_fixture_patch_precommit_conflict_preserves_other_targets(tmp_path, monkeypatch, replacement):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"before\n")
    b.write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    original_fsync = os.fsync
    staged = 0

    def after_staging(fd):
        nonlocal staged
        original_fsync(fd)
        staged += 1
        if staged == 2:
            if replacement == "bytes":
                b.write_bytes(b"concurrent fixture change\n")
            else:
                b.rename(tmp_path / "saved.txt")
                b.write_bytes(b"before\n")

    monkeypatch.setattr(os, "fsync", after_staging)
    with pytest.raises(PatchApplyError, match="^patch_conflict$") as caught:
        direct(tool, patch_for("a.txt", "b.txt"))
    assert caught.value.replaced_paths == ()
    assert a.read_bytes() == b"before\n"
    assert b.read_bytes() == (b"concurrent fixture change\n" if replacement == "bytes" else b"before\n")
    assert staging_names(tmp_path) == []


def test_fixture_patch_staging_collision_preserves_existing_file(tmp_path, monkeypatch):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    unrelated_name = f"{PATCH_TEMP_PREFIX}fixture"
    (tmp_path / unrelated_name).write_bytes(b"not owned by operation\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    monkeypatch.setattr(writer, "uuid4", lambda: SimpleNamespace(hex="fixture"))
    with pytest.raises(PatchApplyError, match="^patch_io_failed$"):
        direct(tool, patch_for("target.txt"))
    assert (tmp_path / "target.txt").read_bytes() == b"before\n"
    assert (tmp_path / unrelated_name).read_bytes() == b"not owned by operation\n"
    assert staging_names(tmp_path) == [unrelated_name]


@pytest.mark.parametrize("fail", [False, True], ids=["short-writes", "stage-failure"])
def test_fixture_patch_handles_short_writes_and_closes_staging_descriptors(tmp_path, monkeypatch, fail):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    open_file, close_file, write_file = os.open, os.close, os.write
    active = set()
    opened_staging = []
    writes = 0

    def record_open(path, flags, *args, **kwargs):
        fd = open_file(path, flags, *args, **kwargs)
        active.add(fd)
        if isinstance(path, str) and path.startswith(PATCH_TEMP_PREFIX):
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and args == (0o600,)
            opened_staging.append(path)
        return fd

    def record_close(fd):
        close_file(fd)
        active.remove(fd)

    def short_write(fd, data):
        nonlocal writes
        writes += 1
        if fail and len(opened_staging) == 2:
            raise OSError("synthetic staging error with /absolute/secret")
        return write_file(fd, data[:2])

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", record_open)
        patch.setattr(os, "close", record_close)
        patch.setattr(os, "write", short_write)
        if fail:
            with pytest.raises(PatchApplyError, match="^patch_io_failed$") as caught:
                direct(tool, patch_for("a.txt", "b.txt"))
            assert caught.value.__suppress_context__
        else:
            assert direct(tool, patch_for("a.txt", "b.txt")).changed_files == 2
        assert active == set()

    expected = b"before\n" if fail else b"after\n"
    assert (tmp_path / "a.txt").read_bytes() == (tmp_path / "b.txt").read_bytes() == expected
    assert writes > 2 and len(opened_staging) == 2 and staging_names(tmp_path) == []


@pytest.mark.parametrize("fail_at", ["second-replace", "immediate-recheck"])
def test_fixture_patch_reports_partial_application_without_rollback(tmp_path, monkeypatch, fail_at):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"before\n")
    b.write_bytes(b"before\n")
    tool = create_fixture_patch_tool(root=tmp_path, context=context_for())
    original = os.replace
    called = []

    def replace_fixture(source, target, *, src_dir_fd, dst_dir_fd):
        called.append(target)
        if target == "b.txt":
            raise OSError("synthetic replacement failure")
        original(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if fail_at == "immediate-recheck":
            b.write_bytes(b"external fixture change\n")

    monkeypatch.setattr(os, "replace", replace_fixture)
    with pytest.raises(PatchApplyError, match="^patch_partial_apply$") as caught:
        direct(tool, patch_for("a.txt", "b.txt"))
    assert caught.value.replaced_paths == ("a.txt",)
    assert called == (["a.txt", "b.txt"] if fail_at == "second-replace" else ["a.txt"])
    assert a.read_bytes() == b"after\n"
    assert b.read_bytes() == (b"before\n" if fail_at == "second-replace" else b"external fixture change\n")
    assert staging_names(tmp_path) == []


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("fully-authorized", ToolErrorCode.EXECUTION_BLOCKED),
        ("second-denied", ToolErrorCode.PERMISSION_DENIED),
        ("no-write-capability", ToolErrorCode.PERMISSION_DENIED),
        ("equal-but-distinct-context", ToolErrorCode.PERMISSION_DENIED),
        ("malformed", ToolErrorCode.INVALID_INPUT),
    ],
)
def test_normal_executor_never_admits_actual_patch_tool(tmp_path, monkeypatch, case, code):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"before\n")
    if case == "second-denied":
        context = context_for(write=(PathScope("a.txt"),))
    elif case == "no-write-capability":
        context = context_for(capabilities={Capability.REPOSITORY_READ})
    else:
        context = context_for()
    tool = create_fixture_patch_tool(root=tmp_path, context=context)
    executor = ToolExecutor(ToolRegistry([tool]))

    def no_write(*_args, **_kwargs):
        pytest.fail("The write handler must not reach the fixture writer")

    async def no_admission():
        pytest.fail("The before_handler callback must not run")

    monkeypatch.setattr(SafeRepositoryWriter, "apply", no_write)
    supplied = replace(context) if case == "equal-but-distinct-context" else context
    text = "malformed" if case == "malformed" else patch_for("a.txt", "b.txt")
    with pytest.raises(ToolExecutionError) as caught:
        asyncio.run(executor.execute("apply_patch", {"patch": text}, context=supplied, before_handler=no_admission))
    assert caught.value.code is code
    assert (tmp_path / "a.txt").read_bytes() == (tmp_path / "b.txt").read_bytes() == b"before\n"
    assert staging_names(tmp_path) == []
