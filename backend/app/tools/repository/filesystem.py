import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.permissions.context import PermissionContext
from app.permissions.paths import parse_logical_path
from app.permissions.policy import PermissionOutcome, PermissionPolicy
from app.tools.contracts import ToolSpec


MAX_PATH_DEPTH = 16
MAX_FILE_BYTES = 64 * 1024
MAX_DIRECTORY_ENTRIES = 256
MAX_EXAMINED_ENTRIES = 4096
MAX_SEARCH_FILES = 64
MAX_SEARCH_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


class RepositoryError(RuntimeError):
    """A safe internal failure while reading a fixture repository."""


class _UnsupportedText(RepositoryError):
    """An authorized regular file that cannot be read as bounded UTF-8 text."""


@dataclass(slots=True)
class _WorkBudget:
    examined_entries: int = 0
    read_bytes_remaining: int | None = None
    read_limit_error: str = "repository_read_byte_budget_exceeded"

    def examine_entry(self) -> None:
        self.examined_entries += 1
        if self.examined_entries > MAX_EXAMINED_ENTRIES:
            raise RepositoryError("repository_entry_budget_exceeded")


@dataclass(frozen=True, slots=True)
class _FileEntry:
    path: str
    parent: int
    name: str
    info: os.stat_result


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _open_fd(path: str | Path, flags: int, *, parent: int | None = None) -> Iterator[int]:
    fd = os.open(path, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _repository_io() -> Iterator[None]:
    try:
        yield
    except OSError:
        raise RepositoryError("repository_io_failed") from None


def check_output_size(size_bytes: int) -> None:
    if size_bytes > MAX_OUTPUT_BYTES:
        raise RepositoryError("repository_output_budget_exceeded")


class SafeRepositoryReader:
    """Descriptor-relative access for a backend-controlled local fixture root."""

    def __init__(self, root: Path, context: PermissionContext) -> None:
        if not isinstance(root, Path) or type(context) is not PermissionContext:
            raise TypeError("Fixture reader requires a Path and a PermissionContext")
        if (
            os.name != "posix"
            or not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
            or os.scandir not in os.supports_fd
        ):
            raise RepositoryError("repository_primitives_unavailable")

        with _repository_io():
            initial = root.lstat()
            if not stat.S_ISDIR(initial.st_mode):
                raise RepositoryError("repository_root_not_directory")
            canonical = root.resolve(strict=True)
            with _open_fd(canonical, _directory_flags()) as fd:
                self._verify_directory(fd, initial)

        self._root = canonical
        self._root_info = initial
        self._context = context
        self._policy = PermissionPolicy()

    def read_text(self, path: str, *, spec: ToolSpec) -> tuple[str, int]:
        components = self._authorized_components(path, spec)
        if not components:
            raise RepositoryError("repository_file_required")

        budget = _WorkBudget()
        with _repository_io(), self._open_directory(components[:-1], budget) as parent:
            name = components[-1]
            info = self._exact_entry(parent, name, budget)
            return self._read_entry(_FileEntry(path, parent, name, info), budget)

    def list_files(self, directory: str, *, spec: ToolSpec) -> tuple[str, ...]:
        paths: list[str] = []
        payload_size = 0
        with self._file_entries(directory, spec, _WorkBudget()) as entries:
            for entry in entries:
                payload_size += len(entry.path.encode("utf-8"))
                check_output_size(payload_size)
                paths.append(entry.path)
        return tuple(sorted(paths))

    @contextmanager
    def search_texts(
        self, directory: str, *, spec: ToolSpec
    ) -> Iterator[Iterator[tuple[str, str | None]]]:
        budget = _WorkBudget(
            read_bytes_remaining=MAX_SEARCH_BYTES, read_limit_error="repository_search_byte_budget_exceeded",
        )
        with self._file_entries(directory, spec, budget) as entries:
            with closing(self._read_candidates(entries, budget)) as texts:
                yield texts

    def _read_candidates(
        self, entries: Iterator[_FileEntry], budget: _WorkBudget
    ) -> Iterator[tuple[str, str | None]]:
        for count, entry in enumerate(entries, start=1):
            if count > MAX_SEARCH_FILES:
                raise RepositoryError("repository_search_file_budget_exceeded")
            try:
                text, _ = self._read_entry(entry, budget)
            except _UnsupportedText:
                text = None
            yield entry.path, text

    def _permits(self, path: str, spec: ToolSpec) -> bool:
        decision = self._policy.evaluate(spec, self._context, paths=(path,))
        return decision.outcome is PermissionOutcome.ALLOW

    def _authorized_components(self, path: str, spec: ToolSpec) -> tuple[str, ...]:
        if not self._permits(path, spec):
            raise RepositoryError("repository_path_denied")
        components = parse_logical_path(path)
        self._check_depth(components)
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise RepositoryError("repository_name_not_utf8") from None
        return components

    @staticmethod
    def _check_depth(components: tuple[str, ...]) -> None:
        if len(components) > MAX_PATH_DEPTH:
            raise RepositoryError("repository_depth_budget_exceeded")

    @contextmanager
    def _open_directory(self, components: tuple[str, ...], budget: _WorkBudget) -> Iterator[int]:
        with ExitStack() as stack:
            fd = stack.enter_context(_open_fd(self._root, _directory_flags()))
            self._verify_directory(fd, self._root_info)
            for name in components:
                info = self._exact_entry(fd, name, budget)
                fd = stack.enter_context(self._open_child_directory(fd, name, info))
            yield fd

    @staticmethod
    def _verify_directory(fd: int, expected: os.stat_result) -> None:
        actual = os.fstat(fd)
        if not stat.S_ISDIR(actual.st_mode) or _identity(actual) != _identity(expected):
            raise RepositoryError("repository_directory_changed")

    @contextmanager
    def _open_child_directory(self, parent: int, name: str, info: os.stat_result) -> Iterator[int]:
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != self._root_info.st_dev:
            raise RepositoryError("repository_directory_refused")
        with _open_fd(name, _directory_flags(), parent=parent) as fd:
            self._verify_directory(fd, info)
            yield fd

    def _entry_names(self, parent: int, budget: _WorkBudget) -> list[str]:
        names: list[str] = []
        # Open a separate directory stream so repeated lookups start at the beginning.
        with _open_fd(".", _directory_flags(), parent=parent) as scan_fd:
            self._verify_directory(scan_fd, os.fstat(parent))
            with os.scandir(scan_fd) as entries:
                for entry in entries:
                    budget.examine_entry()
                    if len(names) == MAX_DIRECTORY_ENTRIES:
                        raise RepositoryError("repository_directory_budget_exceeded")
                    names.append(entry.name)
        return names

    def _exact_entry(self, parent: int, name: str, budget: _WorkBudget) -> os.stat_result:
        if name not in self._entry_names(parent, budget):
            raise RepositoryError("repository_entry_not_found")
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    @contextmanager
    def _file_entries(
        self, directory: str, spec: ToolSpec, budget: _WorkBudget
    ) -> Iterator[Iterator[_FileEntry]]:
        components = self._authorized_components(directory, spec)
        with _repository_io(), self._open_directory(components, budget) as fd:
            with closing(self._walk(fd, components, spec, budget)) as entries:
                yield entries

    def _walk(
        self, fd: int, components: tuple[str, ...], spec: ToolSpec, budget: _WorkBudget
    ) -> Iterator[_FileEntry]:
        for name in sorted(self._entry_names(fd, budget)):
            child = (*components, name)
            path = "/".join(child)
            if not self._permits(path, spec):
                continue
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                continue
            self._check_depth(child)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != self._root_info.st_dev:
                continue
            if stat.S_ISDIR(info.st_mode):
                with self._open_child_directory(fd, name, info) as child_fd:
                    yield from self._walk(child_fd, child, spec, budget)
            elif self._is_readable_file(info):
                yield _FileEntry(path, fd, name, info)

    def _is_readable_file(self, info: os.stat_result) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_dev == self._root_info.st_dev
            and info.st_nlink == 1
        )

    def _read_entry(self, entry: _FileEntry, budget: _WorkBudget) -> tuple[str, int]:
        data, _ = self._read_entry_bytes(entry, budget)
        if b"\0" in data:
            raise _UnsupportedText("repository_text_contains_nul")
        try:
            return data.decode("utf-8", errors="strict"), len(data)
        except UnicodeDecodeError:
            raise _UnsupportedText("repository_text_not_utf8") from None

    def _read_entry_bytes(self, entry: _FileEntry, budget: _WorkBudget) -> tuple[bytes, os.stat_result]:
        if not self._is_readable_file(entry.info):
            raise RepositoryError("repository_file_refused")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with _open_fd(entry.name, flags, parent=entry.parent) as fd:
            opened = os.fstat(fd)
            if not self._is_readable_file(opened) or _identity(opened) != _identity(entry.info):
                raise RepositoryError("repository_file_changed")
            data = self._read_bytes(fd, opened.st_size, budget)
            current = os.fstat(fd)
            if not self._is_readable_file(current):
                raise RepositoryError("repository_file_changed")
        return bytes(data), current

    @staticmethod
    def _read_bytes(fd: int, size: int, budget: _WorkBudget) -> bytearray:
        if size > MAX_FILE_BYTES:
            raise _UnsupportedText("repository_file_budget_exceeded")
        if budget.read_bytes_remaining is not None and size > budget.read_bytes_remaining:
            raise RepositoryError(budget.read_limit_error)

        data = bytearray()
        while True:
            remaining = MAX_FILE_BYTES - len(data)
            if budget.read_bytes_remaining is not None:
                remaining = min(remaining, budget.read_bytes_remaining)
            # A one-byte probe distinguishes exact-limit EOF from growth beyond the limit.
            chunk = os.read(fd, min(8192, remaining + 1))
            if not chunk:
                return data
            if budget.read_bytes_remaining is not None:
                budget.read_bytes_remaining -= len(chunk)
                if budget.read_bytes_remaining < 0:
                    raise RepositoryError(budget.read_limit_error)
            if len(data) + len(chunk) > MAX_FILE_BYTES:
                raise _UnsupportedText("repository_file_budget_exceeded")
            data.extend(chunk)
