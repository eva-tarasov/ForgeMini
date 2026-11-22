"""Snapshot of a repository and the Changeset an agent made on top of it.

Why this exists instead of `git diff` inside the container:
  * the sandbox image may not contain git/patch (python:3.12-slim has neither);
  * the Verifier must test EXACTLY what the agent changed, so the unit of truth is a
    content-addressed changeset (sha256), and the human-readable unified diff is derived from it.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import os
import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", ".tox",
})
IGNORED_SUFFIXES = (".pyc", ".pyo")

MAX_FILES = 5_000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024


class SnapshotError(ValueError):
    """Repository is too large / malformed to be copied into a sandbox."""


def is_ignored(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return any(p in IGNORED_DIR_NAMES for p in parts[:-1]) or parts[-1] in IGNORED_DIR_NAMES \
        or rel_path.endswith(IGNORED_SUFFIXES)


def _safe_rel(name: str) -> str | None:
    """Normalise a tar member name; None if it would escape the workspace."""
    norm = posixpath.normpath(name)
    if norm in (".", "") or norm.startswith("/") or norm == ".." or norm.startswith("../"):
        return None
    return norm


@dataclass(frozen=True, slots=True)
class FileEntry:
    data: bytes
    mode: int  # permission bits only (0o644, 0o755, ...)


@dataclass(frozen=True, slots=True)
class Snapshot:
    files: dict[str, FileEntry] = field(default_factory=dict)

    # ---- construction -------------------------------------------------
    @classmethod
    def from_directory(cls, root: Path) -> Snapshot:
        root = Path(root)
        if not root.is_dir():
            raise SnapshotError(f"not a directory: {root}")
        files: dict[str, FileEntry] = {}
        total = 0
        # followlinks=False + explicit symlink skip: a symlink inside the repo must never
        # pull a host file (e.g. ~/.ssh/id_rsa) into the sandbox.
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIR_NAMES
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for fname in sorted(filenames):
                full = Path(dirpath) / fname
                rel = full.relative_to(root).as_posix()
                if full.is_symlink() or is_ignored(rel):
                    continue
                size = full.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise SnapshotError(f"file too large ({size} bytes): {rel}")
                total += size
                if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise SnapshotError("repository too large for the sandbox")
                files[rel] = FileEntry(full.read_bytes(), full.stat().st_mode & 0o777)
        return cls(files)

    @classmethod
    def from_tar(cls, raw: bytes) -> Snapshot:
        files: dict[str, FileEntry] = {}
        total = 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for member in tar:
                if not member.isfile():  # skip dirs, symlinks, devices
                    continue
                rel = _safe_rel(member.name)
                if rel is None or is_ignored(rel):
                    continue
                total += member.size
                if member.size > MAX_FILE_BYTES or len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise SnapshotError(f"workspace export too large at {rel}")
                fh = tar.extractfile(member)
                files[rel] = FileEntry(fh.read() if fh else b"", member.mode & 0o777)
        return cls(files)

    # ---- serialisation ------------------------------------------------
    def to_tar(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tar:
            for rel in sorted(self.files):
                entry = self.files[rel]
                info = tarfile.TarInfo(rel)
                info.size = len(entry.data)
                info.mode = entry.mode
                info.mtime = 0
                tar.addfile(info, io.BytesIO(entry.data))
        return buf.getvalue()

    def sha256(self) -> str:
        """Fingerprint of the whole tree; lets the Verifier detect that the source moved under a run."""
        h = hashlib.sha256()
        for p in sorted(self.files):
            e = self.files[p]
            h.update(p.encode() + b"\0" + str(e.mode).encode() + b"\0" + hashlib.sha256(e.data).digest())
        return h.hexdigest()

    # ---- comparison ---------------------------------------------------
    def changes_to(self, other: Snapshot) -> Changeset:
        """What must be done to `self` to obtain `other`."""
        written = {p: e for p, e in other.files.items() if self.files.get(p) != e}
        deleted = tuple(sorted(p for p in self.files if p not in other.files))
        return Changeset(written=written, deleted=deleted)


@dataclass(frozen=True, slots=True)
class Changeset:
    written: dict[str, FileEntry] = field(default_factory=dict)  # created or modified
    deleted: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.written and not self.deleted

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.written, *self.deleted}))

    def apply_to(self, base: Snapshot) -> Snapshot:
        files = {p: e for p, e in base.files.items() if p not in self.deleted}
        files.update(self.written)
        return Snapshot(files)

    def sha256(self) -> str:
        """Stable fingerprint: same changeset <=> same hash, independent of dict order."""
        h = hashlib.sha256()
        for p in sorted(self.written):
            e = self.written[p]
            h.update(b"W\0" + p.encode() + b"\0" + str(e.mode).encode() + b"\0"
                     + hashlib.sha256(e.data).digest())
        for p in sorted(self.deleted):
            h.update(b"D\0" + p.encode() + b"\0")
        return h.hexdigest()

    def unified_diff(self, base: Snapshot) -> str:
        chunks: list[str] = []
        for path in self.paths:
            old = base.files.get(path)
            new = self.written.get(path)
            if old is not None and new is not None and old.data == new.data:
                continue  # mode-only change: not representable in a plain diff
            old_txt = _decode(old.data) if old else ""
            new_txt = _decode(new.data) if new else ""
            if old_txt is None or new_txt is None:
                chunks.append(f"Binary files a/{path} and b/{path} differ\n")
                continue
            chunks.append("".join(difflib.unified_diff(
                old_txt.splitlines(keepends=True), new_txt.splitlines(keepends=True),
                fromfile=f"a/{path}" if old else "/dev/null",
                tofile=f"b/{path}" if new else "/dev/null",
            )))
        return "".join(chunks)

    # ---- persistence (artifact on disk, read back by the Verifier) -----
    def to_json(self) -> str:
        return json.dumps({
            "written": {p: {"mode": e.mode, "b64": base64.b64encode(e.data).decode()}
                        for p, e in sorted(self.written.items())},
            "deleted": list(self.deleted),
        }, indent=1)

    @classmethod
    def from_json(cls, raw: str) -> Changeset:
        obj = json.loads(raw)
        written = {}
        for p, v in obj["written"].items():
            if _safe_rel(p) is None:
                raise SnapshotError(f"unsafe path in changeset: {p!r}")
            written[p] = FileEntry(base64.b64decode(v["b64"]), int(v["mode"]))
        deleted = tuple(obj["deleted"])
        if any(_safe_rel(p) is None for p in deleted):
            raise SnapshotError("unsafe path in changeset")
        return cls(written=written, deleted=deleted)


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
