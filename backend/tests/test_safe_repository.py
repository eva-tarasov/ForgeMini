import asyncio
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.tools.contracts import ToolRiskLevel, ToolSpec
from app.tools.repository import filesystem as fs
from app.tools.repository.schemas import ReadFileInput, ReadFileOutput


READ_SPEC = ToolSpec(
    name="sample_read",
    version=1,
    description="Read an authorized temporary test fixture.",
    input_schema=ReadFileInput,
    output_schema=ReadFileOutput,
    capability=Capability.REPOSITORY_READ,
    risk_level=ToolRiskLevel.LOW,
    timeout_ms=2000,
    side_effect=False,
    requires_approval=False,
)


def reader_for(root: Path, *scopes: PathScope) -> fs.SafeRepositoryReader:
    context = PermissionContext(
        UUID(int=1), UUID(int=2), frozenset({Capability.REPOSITORY_READ}), read_scopes=scopes
    )
    return fs.SafeRepositoryReader(root, context)


def test_reader_preserves_bytes_and_allows_exact_file_without_ancestor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    content = "Привет\r\nsecond\nlast"
    data = content.encode("utf-8")
    (tmp_path / "src" / "one.txt").write_bytes(data)
    reader = reader_for(tmp_path, PathScope("src/one.txt"))
    original_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original_read(fd, min(size, 3)))

    assert reader.read_text("src/one.txt", spec=READ_SPEC) == (content, len(data))
    with pytest.raises(fs.RepositoryError, match="repository_path_denied"):
        reader.list_files("src", spec=READ_SPEC)


@pytest.mark.parametrize("path", ["../outside.txt", "/absolute", "src//file.txt", ".GiT/config", ".eNv.local"])
def test_reader_denies_logical_paths_before_opening_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    reader = reader_for(tmp_path, PathScope(".", recursive=True))

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("Denied input must not reopen the root")

    monkeypatch.setattr(os, "open", unexpected_open)
    with pytest.raises(fs.RepositoryError, match="repository_path_denied"):
        reader.read_text(path, spec=READ_SPEC)


def test_reader_refuses_symlinks_and_hard_links(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("synthetic outside sentinel", encoding="utf-8")
    inside = root / "inside.txt"
    inside.write_text("synthetic inside sentinel", encoding="utf-8")
    (root / "outside-link").symlink_to(sentinel)
    (root / "inside-link").symlink_to(inside)
    (root / "intermediate").symlink_to(outside, target_is_directory=True)
    os.link(sentinel, root / "hard-link")
    reader = reader_for(root, PathScope(".", recursive=True))

    for path in ("outside-link", "inside-link", "intermediate/sentinel.txt", "hard-link"):
        with pytest.raises(fs.RepositoryError, match="repository_(file|directory)_refused"):
            reader.read_text(path, spec=READ_SPEC)

    assert reader.list_files(".", spec=READ_SPEC) == ("inside.txt",)


def test_reader_refuses_fifo_and_directory_without_opening_as_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "directory").mkdir()
    reader = reader_for(tmp_path, PathScope(".", recursive=True))
    original_open = os.open

    def checked_open(path, flags, *, dir_fd=None):
        if path in {"pipe", "directory"}:
            pytest.fail("Special files and directories must be rejected by the pre-check")
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", checked_open)
    for path in ("pipe", "directory"):
        with pytest.raises(fs.RepositoryError, match="repository_file_refused"):
            reader.read_text(path, spec=READ_SPEC)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\xffinvalid", "repository_text_not_utf8"),
        (b"abc\0def", "repository_text_contains_nul"),
        (b"x" * (fs.MAX_FILE_BYTES + 1), "repository_file_budget_exceeded"),
    ],
    ids=["invalid-utf8", "nul", "oversize"],
)
def test_reader_refuses_unsupported_text(
    tmp_path: Path, data: bytes, reason: str
) -> None:
    (tmp_path / "file.txt").write_bytes(data)
    reader = reader_for(tmp_path, PathScope("file.txt"))

    with pytest.raises(fs.RepositoryError, match=f"^{reason}$"):
        reader.read_text("file.txt", spec=READ_SPEC)


def test_reader_enforces_actual_byte_limit_during_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "growing.txt"
    fixture.write_bytes(b"x" * fs.MAX_FILE_BYTES)
    reader = reader_for(tmp_path, PathScope("growing.txt"))
    assert reader.read_text("growing.txt", spec=READ_SPEC)[1] == fs.MAX_FILE_BYTES
    original_read = os.read
    grew = False

    def read_after_growth(fd: int, size: int) -> bytes:
        nonlocal grew
        if not grew:
            with fixture.open("ab") as stream:
                stream.write(b"!")
            grew = True
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", read_after_growth)
    with pytest.raises(fs.RepositoryError, match="repository_file_budget_exceeded"):
        reader.read_text("growing.txt", spec=READ_SPEC)


def test_reader_requires_exact_entry_names(tmp_path: Path) -> None:
    (tmp_path / "Source").mkdir()
    (tmp_path / "Source" / "Exact.txt").write_text("fixture", encoding="utf-8")
    reader = reader_for(tmp_path, PathScope(".", recursive=True))

    assert reader.read_text("Source/Exact.txt", spec=READ_SPEC) == ("fixture", 7)
    for path in ("source/Exact.txt", "Source/exact.txt"):
        with pytest.raises(fs.RepositoryError, match="repository_entry_not_found"):
            reader.read_text(path, spec=READ_SPEC)


def test_reader_fails_closed_without_descriptor_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "supports_fd", os.supports_fd - {os.scandir})
    with pytest.raises(fs.RepositoryError, match="repository_primitives_unavailable"):
        reader_for(tmp_path, PathScope(".", recursive=True))


def test_reader_rejects_explicit_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(fs.RepositoryError, match="repository_root_not_directory"):
        reader_for(link, PathScope(".", recursive=True))


@pytest.mark.parametrize("symlink_replacement", [False, True], ids=["directory", "symlink"])
def test_reader_rejects_root_replacement(tmp_path: Path, symlink_replacement: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "same.txt").write_text("original", encoding="utf-8")
    reader = reader_for(root, PathScope(".", recursive=True))
    root.rename(tmp_path / "parked")
    replacement = tmp_path / "replacement" if symlink_replacement else root
    replacement.mkdir()
    (replacement / "same.txt").write_text("replacement", encoding="utf-8")
    if symlink_replacement:
        root.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(fs.RepositoryError, match="repository_(directory_changed|io_failed)"):
        reader.read_text("same.txt", spec=READ_SPEC)


def test_reader_refuses_intermediate_symlink_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "src").mkdir()
    (outside / "file.txt").write_text("synthetic outside sentinel", encoding="utf-8")
    reader = reader_for(root, PathScope(".", recursive=True))
    original_open = os.open
    replaced = False

    def open_after_substitution(path, flags, *, dir_fd=None):
        nonlocal replaced
        if path == "src" and not replaced:
            (root / "src").rename(root / "parked")
            (root / "src").symlink_to(outside, target_is_directory=True)
            replaced = True
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_after_substitution)
    with pytest.raises(fs.RepositoryError, match="repository_io_failed"):
        reader.read_text("src/file.txt", spec=READ_SPEC)
    assert replaced


def test_reader_closes_descriptors_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "good").write_bytes(b"ok")
    (tmp_path / "src" / "bad").write_bytes(b"\0")
    reader = reader_for(tmp_path, PathScope(".", recursive=True))
    original_open, original_close = os.open, os.close
    active: set[int] = set()
    file_opens = []

    def capture_open(path, flags, *, dir_fd=None):
        fd = original_open(path, flags, dir_fd=dir_fd)
        active.add(fd)
        if path in ("good", "bad"):
            file_opens.append((flags, dir_fd))
        return fd

    def capture_close(fd: int) -> None:
        original_close(fd)
        active.remove(fd)

    cancelled = asyncio.CancelledError()

    def cancelled_read(_fd: int, _size: int) -> bytes:
        raise cancelled

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", capture_open)
        patch.setattr(os, "close", capture_close)
        assert reader.read_text("src/good", spec=READ_SPEC) == ("ok", 2)
        assert active == set()
        with pytest.raises(fs.RepositoryError, match="repository_text_contains_nul"):
            reader.read_text("src/bad", spec=READ_SPEC)
        assert active == set()
        patch.setattr(os, "read", cancelled_read)
        with pytest.raises(asyncio.CancelledError) as caught:
            reader.read_text("src/good", spec=READ_SPEC)
        assert caught.value is cancelled
        assert active == set()

    assert len(file_opens) == 3
    for flags, parent in file_opens:
        assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        assert parent is not None


def test_reader_bounds_directory_iteration_before_materializing_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(fs.MAX_DIRECTORY_ENTRIES + 5):
        (tmp_path / f"entry-{index:03}").touch()
    reader = reader_for(tmp_path, PathScope(".", recursive=True))
    original_scandir = os.scandir
    consumed = 0

    @contextmanager
    def counted_scandir(fd):
        with original_scandir(fd) as entries:
            def count_entries():
                nonlocal consumed
                for entry in entries:
                    consumed += 1
                    yield entry

            yield count_entries()

    monkeypatch.setattr(os, "scandir", counted_scandir)
    with pytest.raises(fs.RepositoryError, match="repository_directory_budget_exceeded"):
        reader.list_files(".", spec=READ_SPEC)
    assert consumed == fs.MAX_DIRECTORY_ENTRIES + 1


def test_reader_counts_repeated_component_lookups_against_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "sibling").touch()
    (tmp_path / "src" / "file").touch()
    (tmp_path / "src" / "extra").touch()
    reader = reader_for(tmp_path, PathScope(".", recursive=True))
    monkeypatch.setattr(fs, "MAX_EXAMINED_ENTRIES", 3)

    with pytest.raises(fs.RepositoryError, match="repository_entry_budget_exceeded"):
        reader.read_text("src/file", spec=READ_SPEC)


def test_reader_enforces_path_depth_for_direct_read_and_discovery(tmp_path: Path) -> None:
    directory = tmp_path.joinpath(*("a",) * fs.MAX_PATH_DEPTH)
    directory.mkdir(parents=True)
    (directory / "file").touch()
    path = (directory / "file").relative_to(tmp_path).as_posix()
    reader = reader_for(tmp_path, PathScope(".", recursive=True))

    with pytest.raises(fs.RepositoryError, match="repository_depth_budget_exceeded"):
        reader.read_text(path, spec=READ_SPEC)
    with pytest.raises(fs.RepositoryError, match="repository_depth_budget_exceeded"):
        reader.list_files(".", spec=READ_SPEC)


def test_reader_does_not_inspect_or_open_denied_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "one.txt").write_text("allowed", encoding="utf-8")
    for directory in ("private", ".GiT"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "never.txt").write_text("synthetic hidden text", encoding="utf-8")
    (tmp_path / ".eNv.local").write_text("synthetic hidden text", encoding="utf-8")
    reader = reader_for(tmp_path, PathScope("."), PathScope("allowed", recursive=True))
    original_stat, original_open = os.stat, os.open
    denied_names = {"private", ".GiT", ".eNv.local", "never.txt"}

    def checked_stat(path, *args, **kwargs):
        if isinstance(path, str) and path in denied_names:
            pytest.fail("A denied child should be filtered before stat")
        return original_stat(path, *args, **kwargs)

    def checked_open(path, flags, *, dir_fd=None):
        if isinstance(path, str) and path in denied_names:
            pytest.fail("A denied child should not be opened")
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "stat", checked_stat)
    monkeypatch.setattr(os, "open", checked_open)
    assert reader.list_files(".", spec=READ_SPEC) == ("allowed/one.txt",)
    with reader.search_texts(".", spec=READ_SPEC) as texts:
        assert list(texts) == [("allowed/one.txt", "allowed")]


def test_reader_refuses_wrong_device_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").touch()
    reader = reader_for(tmp_path, PathScope(".", recursive=True))
    original_stat = os.stat

    def wrong_device(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path != "file":
            return info
        values = list(info)
        values[2] += 1  # st_dev
        return os.stat_result(values)

    monkeypatch.setattr(os, "stat", wrong_device)
    with pytest.raises(fs.RepositoryError, match="repository_file_refused"):
        reader.read_text("file", spec=READ_SPEC)
    assert reader.list_files(".", spec=READ_SPEC) == ()
