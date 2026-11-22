"""`forge solve --apply`: a verified patch goes into the working tree with git; everything else is refused."""

import subprocess
from pathlib import Path

import pytest

from forge_cli.apply import ApplyError, apply_patch


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


A = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
B = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-y = 1\n+y = 2\n"
B_STALE = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-y = 999\n+y = 2\n"


def test_applies_a_clean_patch(repo):
    apply_patch(repo, A + B)
    assert (repo / "a.py").read_text() == "x = 2\n" and (repo / "b.py").read_text() == "y = 2\n"


def test_all_or_nothing_when_one_file_no_longer_matches(repo):
    with pytest.raises(ApplyError, match="--check failed"):
        apply_patch(repo, A + B_STALE)
    assert (repo / "a.py").read_text() == "x = 1\n"                  # the good half was NOT applied either


@pytest.mark.parametrize("diff", [
    "diff --git a/.git/hooks/pre-commit b/.git/hooks/pre-commit\nnew file mode 100755\n--- /dev/null\n"
    "+++ b/.git/hooks/pre-commit\n@@ -0,0 +1 @@\n+rm -rf ~\n",
    "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n",
    "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc/passwd\n",
    "diff --git a/sub b/sub\nnew file mode 160000\nindex 0000000..1234567\n",
])
def test_refuses_git_dir_traversal_symlinks_and_submodules(repo, diff):
    with pytest.raises(ApplyError, match="refusing"):
        apply_patch(repo, diff)
    assert git(repo, "status", "--porcelain") == ""


def test_empty_patch_is_an_error(repo):
    with pytest.raises(ApplyError, match="empty"):
        apply_patch(repo, "  \n")
