import os

import pytest

from app.sandbox.changeset import Changeset, FileEntry, Snapshot, SnapshotError


def snap(**files: str) -> Snapshot:
    return Snapshot({p.replace("__", "/"): FileEntry(c.encode(), 0o644) for p, c in files.items()})


def test_changes_to_reports_modified_created_deleted():
    a = snap(x="1\n", y="2\n", z="3\n")
    b = snap(x="1\n", y="changed\n", new="n\n")
    cs = a.changes_to(b)
    assert sorted(cs.written) == ["new", "y"]
    assert cs.deleted == ("z",)
    assert cs.apply_to(a).files == b.files          # applying the changeset reproduces the target


def test_empty_changeset_when_nothing_changed():
    a = snap(x="1\n")
    assert a.changes_to(a).is_empty


def test_hash_is_stable_and_content_sensitive():
    a = snap(x="1\n")
    one = a.changes_to(snap(x="2\n"))
    again = a.changes_to(snap(x="2\n"))
    other = a.changes_to(snap(x="3\n"))
    assert one.sha256() == again.sha256() != other.sha256()


def test_unified_diff_shape_for_modify_create_delete():
    a = snap(calc="def add(a, b):\n    return a - b\n", gone="bye\n")
    b = snap(calc="def add(a, b):\n    return a + b\n", fresh="hi\n")
    diff = a.changes_to(b).unified_diff(a)
    assert "--- a/calc\n+++ b/calc" in diff
    assert "-    return a - b" in diff and "+    return a + b" in diff
    assert "--- /dev/null\n+++ b/fresh" in diff
    assert "--- a/gone\n+++ /dev/null" in diff


def test_binary_file_is_reported_not_decoded():
    a = Snapshot({"img": FileEntry(b"\xff\xfe\x00", 0o644)})
    b = Snapshot({"img": FileEntry(b"\xff\xfe\x01", 0o644)})
    assert "Binary files a/img and b/img differ" in a.changes_to(b).unified_diff(a)


def test_json_roundtrip_preserves_bytes_and_modes():
    cs = Changeset(written={"s.sh": FileEntry(b"#!/bin/sh\n\xff", 0o755)}, deleted=("old",))
    back = Changeset.from_json(cs.to_json())
    assert back == cs and back.sha256() == cs.sha256()


@pytest.mark.parametrize("bad", ["../evil", "/etc/passwd", ".."])
def test_from_json_rejects_paths_that_escape_the_workspace(bad):
    cs = Changeset(written={bad: FileEntry(b"x", 0o644)})
    with pytest.raises(SnapshotError):
        Changeset.from_json(cs.to_json())


def test_tar_roundtrip_and_ignored_names(tmp_path):
    s = Snapshot({"a.py": FileEntry(b"1", 0o644), "__pycache__/a.pyc": FileEntry(b"x", 0o644),
                  "pkg/b.py": FileEntry(b"2", 0o755)})
    back = Snapshot.from_tar(s.to_tar())
    assert sorted(back.files) == ["a.py", "pkg/b.py"]          # __pycache__ dropped on import
    assert back.files["pkg/b.py"].mode == 0o755


def test_from_directory_skips_git_pycache_and_symlinks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text("x=1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("secret")
    (tmp_path / "src/__pycache__").mkdir()
    (tmp_path / "src/__pycache__/m.pyc").write_bytes(b"\0")
    secret = tmp_path.parent / "host_secret.txt"
    secret.write_text("TOP SECRET")
    os.symlink(secret, tmp_path / "link.txt")                 # must NOT leak into the sandbox
    os.symlink(tmp_path.parent, tmp_path / "dirlink")
    assert sorted(Snapshot.from_directory(tmp_path).files) == ["src/m.py"]


def test_from_directory_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.sandbox.changeset.MAX_FILE_BYTES", 10)
    (tmp_path / "big").write_bytes(b"x" * 11)
    with pytest.raises(SnapshotError):
        Snapshot.from_directory(tmp_path)
