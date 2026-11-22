"""`forge solve --apply`: put a VERIFIED patch into the working tree with plain `git apply`.

Only called after the Verifier said PASS. The diff itself is still treated as untrusted (it was produced by a
model inside a sandbox): `git apply` already refuses paths outside the work tree and writes through symlinks;
on top of that we refuse the two things a patch for ordinary source code has no business doing here: touching
`.git/` and creating symlinks/submodules (modes 120000 / 160000). Dry run first, so it is all-or-nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_PATHS = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|(?:---|\+\+\+) [ab]/(\S+))", re.M)
_SPECIAL_MODE = re.compile(r"^(?:new file mode|new mode|old mode|index [0-9a-f]+\.\.[0-9a-f]+) ?(120000|160000)\b", re.M)


class ApplyError(Exception):
    pass


def _touches_git_dir(diff: str) -> bool:
    for groups in _PATHS.findall(diff):
        for path in filter(None, groups):
            parts = Path(path).parts
            if ".git" in parts or ".." in parts:
                return True
    return False


def apply_patch(repo: Path, diff: str) -> None:
    if not diff.strip():
        raise ApplyError("the patch is empty")
    if _touches_git_dir(diff):
        raise ApplyError("the patch touches .git/ or uses '..' in a path; refusing to apply it automatically")
    if _SPECIAL_MODE.search(diff):
        raise ApplyError("the patch creates a symlink or submodule; refusing to apply it automatically")
    env = {**os.environ, "LC_ALL": "C"}
    for argv in (["git", "-C", str(repo), "apply", "--check", "-"], ["git", "-C", str(repo), "apply", "-"]):
        try:
            done = subprocess.run(argv, input=diff, env=env, capture_output=True, text=True, check=False, timeout=60)
        except FileNotFoundError:
            raise ApplyError("git is not installed or not in PATH") from None
        if done.returncode != 0:
            detail = " ".join((done.stderr or done.stdout or "no output").split())[-300:]
            raise ApplyError(f"git apply {'--check ' if '--check' in argv else ''}failed: {detail}. "
                             "Your working tree has changed since the run? Apply it by hand: forge patch RUN_ID")
