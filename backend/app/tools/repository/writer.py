"""Controlled replacement within an exclusively owned disposable POSIX fixture."""

import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.permissions.policy import PermissionOutcome, PermissionPolicy
from app.tools.contracts import ToolSpec
from app.tools.repository.filesystem import (
    RepositoryError,
    SafeRepositoryReader,
    _FileEntry,
    _WorkBudget,
    _identity,
)
from app.tools.repository.patch import (
    PATCH_TEMP_PREFIX,
    FilePatch,
    ParsedPatch,
    PatchTextError,
    apply_file_patch,
    parse_patch,
)


MAX_TOTAL_PATCH_FILE_BYTES = 256 * 1024


class PatchApplyError(RuntimeError):
    """Safe diagnostic for direct fixture callers. Paths never appear in the message."""

    def __init__(self, code: str, replaced_paths: tuple[str, ...] = ()) -> None:
        self.code = code
        self.replaced_paths = replaced_paths
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    patch: FilePatch
    parent: int
    identity: tuple[int, int]
    mode: int
    original: bytes
    replacement: bytes


_StagedFiles = dict[tuple[int, str], tuple[int, int] | None]


class SafeRepositoryWriter:
    """Internal fixture mechanism. Scope authorization leaves human approval pending."""

    def __init__(
        self, root: Path, context: PermissionContext, *, read_spec: ToolSpec, write_spec: ToolSpec,
    ) -> None:
        # POSIX os.replace shares the dir_fd implementation of os.rename.
        if (
            os.rename not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd
            or not all(hasattr(os, name) for name in ("replace", "fchmod", "fsync", "geteuid", "O_EXCL"))
        ):
            raise PatchApplyError("patch_primitives_unavailable")
        if (
            read_spec.capability is not Capability.REPOSITORY_READ
            or write_spec.capability is not Capability.REPOSITORY_WRITE
        ):
            raise PatchApplyError("patch_invalid_tool_specs")
        self._reader = SafeRepositoryReader(root, context)
        self._context = context
        self._policy = PermissionPolicy()
        self._read_spec = read_spec
        self._write_spec = write_spec

    def apply(self, text: str) -> tuple[str, ...]:
        """Prepare, stage and replace in patch order. A failure after replacement is partial."""
        pending: _StagedFiles = {}
        replaced: list[str] = []
        cleanup_failed = False
        try:
            patch = parse_patch(text)
            self._authorize(patch.paths)
            # Preparation, full precommit recheck and immediate recheck share one budget.
            budget = _WorkBudget(read_bytes_remaining=3 * MAX_TOTAL_PATCH_FILE_BYTES)
            with ExitStack() as directories:
                plan = self._prepare(patch, directories, budget)
                try:
                    staging_names = tuple(self._stage(target, pending) for target in plan)
                    for target in plan:
                        self._revalidate(target, budget)
                    for target, name in zip(plan, staging_names, strict=True):
                        self._revalidate(target, budget)
                        key = target.parent, name
                        if not self._owns_staging(key, pending[key]):
                            raise PatchApplyError("patch_conflict")
                        os.replace(
                            name, target.patch.components[-1],
                            src_dir_fd=target.parent, dst_dir_fd=target.parent,
                        )
                        replaced.append(target.patch.path)
                        del pending[key]
                finally:
                    cleanup_failed = self._cleanup(pending)
        except (OSError, RepositoryError, PatchTextError, PatchApplyError) as error:
            code = "patch_io_failed"
            if replaced:
                code = "patch_partial_apply"
            elif isinstance(error, (PatchTextError, PatchApplyError)):
                code = error.code
            failure = PatchApplyError(code, tuple(replaced))
            if cleanup_failed:
                failure.add_note("patch_staging_cleanup_failed")
            raise failure from None
        return tuple(replaced)

    def _authorize(self, paths: tuple[str, ...]) -> None:
        write_outcome = self._policy.evaluate(self._write_spec, self._context, paths=paths).outcome
        read_outcome = self._policy.evaluate(self._read_spec, self._context, paths=paths).outcome
        if write_outcome is not PermissionOutcome.APPROVAL_REQUIRED or read_outcome is not PermissionOutcome.ALLOW:
            raise PatchApplyError("permission_denied")

    def _prepare(
        self, patch: ParsedPatch, directories: ExitStack, budget: _WorkBudget,
    ) -> tuple[_PreparedFile, ...]:
        targets: list[_PreparedFile] = []
        identities: set[tuple[int, int]] = set()
        original_size = replacement_size = 0
        for file in patch.files:
            parent = directories.enter_context(self._reader._open_directory(file.components[:-1], budget))
            entry = self._entry(file, parent, budget)
            mode = self._eligible_mode(entry.info)
            identity = _identity(entry.info)
            if identity in identities:
                raise PatchApplyError("patch_conflict")
            identities.add(identity)
            original, current = self._reader._read_entry_bytes(entry, budget)
            if self._eligible_mode(current) != mode:
                raise PatchApplyError("patch_conflict")
            try:
                source = original.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise PatchApplyError("patch_unsupported_text") from None
            replacement = apply_file_patch(source, file).encode("utf-8")
            original_size += len(original)
            replacement_size += len(replacement)
            if max(original_size, replacement_size) > MAX_TOTAL_PATCH_FILE_BYTES:
                raise PatchApplyError("patch_limit_exceeded")
            targets.append(_PreparedFile(file, parent, identity, mode, original, replacement))
        return tuple(targets)

    def _entry(self, file: FilePatch, parent: int, budget: _WorkBudget) -> _FileEntry:
        name = file.components[-1]
        info = self._reader._exact_entry(parent, name, budget)
        return _FileEntry(file.path, parent, name, info)

    def _eligible_mode(self, info: os.stat_result) -> int:
        mode = stat.S_IMODE(info.st_mode)
        if (
            not self._reader._is_readable_file(info) or info.st_uid != os.geteuid()
            or mode & ~0o777 or not mode & stat.S_IWUSR or getattr(info, "st_flags", 0)
        ):
            raise PatchApplyError("patch_unsupported_target")
        return mode

    def _revalidate(self, target: _PreparedFile, budget: _WorkBudget) -> None:
        try:
            with self._reader._open_directory(target.patch.components[:-1], budget) as parent:
                if _identity(os.fstat(parent)) != _identity(os.fstat(target.parent)):
                    raise PatchApplyError("patch_conflict")
                entry = self._entry(target.patch, parent, budget)
                if _identity(entry.info) != target.identity or self._eligible_mode(entry.info) != target.mode:
                    raise PatchApplyError("patch_conflict")
                current, info = self._reader._read_entry_bytes(entry, budget)
                if current != target.original or self._eligible_mode(info) != target.mode:
                    raise PatchApplyError("patch_conflict")
        except (OSError, RepositoryError, PatchApplyError):
            raise PatchApplyError("patch_conflict") from None

    @staticmethod
    def _stage(target: _PreparedFile, pending: _StagedFiles) -> str:
        name = f"{PATCH_TEMP_PREFIX}{uuid4().hex}"
        key = target.parent, name
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target.parent,
        )
        pending[key] = None
        try:
            pending[key] = _identity(os.fstat(fd))
            remaining = memoryview(target.replacement)
            while remaining:
                written = os.write(fd, remaining)
                if not 0 < written <= len(remaining):
                    raise PatchApplyError("patch_io_failed")
                remaining = remaining[written:]
            os.fchmod(fd, target.mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        return name

    @staticmethod
    def _owns_staging(key: tuple[int, str], identity: tuple[int, int] | None) -> bool:
        parent, name = key
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (
            identity is None or _identity(info) == identity
        )

    def _cleanup(self, pending: _StagedFiles) -> bool:
        failed = False
        for key, identity in pending.items():
            parent, name = key
            try:
                if self._owns_staging(key, identity):
                    os.unlink(name, dir_fd=parent)
                else:
                    failed = True
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
        return failed
