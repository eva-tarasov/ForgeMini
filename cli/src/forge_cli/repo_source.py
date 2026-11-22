"""Where the repository comes from: a local directory, or a git URL that we clone ourselves.

Why clone in the CLI and not on the server: the server stays "give me a path on this machine",
and everything risky about URLs (option injection, credentials in argv, protocol tricks) is
handled in ONE small, testable place. The clone is kept (not temporary) because the Verifier
re-reads the source directory when it re-runs the tests, so it must still exist later.

Threat model, in short (a URL is untrusted input, e.g. copied from an issue or from an agent):
  * a "URL" starting with "-" would be parsed by git as an OPTION (`--upload-pack=<cmd>` runs a
    command)  -> rejected, and we also pass "--" before it;
  * credentials inside a URL end up in shell history, `ps` output and the .git/config -> rejected;
  * exotic transports (`ext::<command>`) execute commands -> only https/ssh/git/file allowed,
    enforced twice: by our parser and by GIT_ALLOW_PROTOCOL for git itself;
  * git must never block on a password prompt -> GIT_TERMINAL_PROMPT=0.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("https", "ssh", "git", "file")
CLONE_TIMEOUT_SECONDS = 180
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s:][^\s]*$")        # git@github.com:owner/repo.git
_CONTROL = re.compile(r"[\x00-\x20\x7f]")


class RepoSourceError(Exception):
    """The --repo value is unusable; the message says what to do (printed as-is)."""


@dataclass(frozen=True)
class ResolvedRepo:
    path: Path
    label: str                      # what to show: the URL, or the local directory name
    cloned_from: str | None = None
    commit: str | None = None       # short HEAD sha of the clone


def looks_like_url(value: str) -> bool:
    return "://" in value or bool(_SCP_LIKE.match(value))


def validate_git_url(value: str) -> str:
    """Return the URL unchanged if it is safe to hand to `git clone`; raise RepoSourceError otherwise."""
    if value.startswith("-"):
        raise RepoSourceError("Repository URL must not start with '-' (git would treat it as an option).")
    if _CONTROL.search(value):
        raise RepoSourceError("Repository URL contains spaces or control characters.")
    if _SCP_LIKE.match(value) and "://" not in value:
        return value
    parsed = urlparse(value)
    if parsed.scheme == "http":
        raise RepoSourceError("Plain http:// is not allowed (no integrity). Use https://.")
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise RepoSourceError(
            f"Unsupported repository URL scheme {parsed.scheme!r}. Allowed: {', '.join(ALLOWED_SCHEMES)}, "
            "or git@host:owner/repo.")
    if parsed.password is not None or (parsed.scheme in ("https", "git") and parsed.username is not None):
        raise RepoSourceError(
            "Do not put credentials in the URL (they leak into shell history and .git/config). "
            "Clone it yourself with your credential helper or SSH key, then pass the local directory.")
    if parsed.scheme != "file" and not parsed.hostname:
        raise RepoSourceError("Repository URL has no host.")
    if parsed.scheme == "file" and not parsed.path:
        raise RepoSourceError("file:// URL has no path.")
    return value


def clones_root() -> Path:
    return Path(os.environ.get("FORGE_CLONES_DIR") or Path.home() / ".forgemini" / "clones")


def _slug(url: str) -> str:
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    return re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip(".-")[:40] or "repo"


def clone_destination(url: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return clones_root() / f"{_slug(url)}-{digest}-{stamp}"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",                                  # never wait for a password on a tty
        "GIT_ALLOW_PROTOCOL": ":".join(ALLOWED_SCHEMES),             # git-level allowlist, second line of defence
        "GIT_ASKPASS": "true",                                       # any askpass attempt answers "" -> fails fast
        "LC_ALL": "C",
    })
    return env


def clone_repo(url: str, *, dest: Path | None = None) -> ResolvedRepo:
    safe = validate_git_url(url)
    target = dest or clone_destination(safe)
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = ["git", "clone", "--depth", "1", "--quiet", "--", safe, str(target)]
    try:
        proc = subprocess.run(argv, env=_git_env(), capture_output=True, text=True,
                              timeout=CLONE_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        raise RepoSourceError("git is not installed or not in PATH; it is needed to clone a repository URL.") from None
    except subprocess.TimeoutExpired:
        raise RepoSourceError(f"git clone did not finish in {CLONE_TIMEOUT_SECONDS}s.") from None
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or proc.stdout or "no output").split())[-400:]
        raise RepoSourceError(
            f"git clone failed (exit {proc.returncode}): {tail}\n"
            "For a private repository clone it yourself first (your SSH key / credential helper) "
            "and pass the local directory.")
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
                          env=_git_env(), capture_output=True, text=True, check=False)
    return ResolvedRepo(path=target.resolve(), label=safe, cloned_from=safe,
                        commit=head.stdout.strip() or None)


def resolve_repo(value: str) -> ResolvedRepo:
    """A directory that exists wins; otherwise a URL-looking value is cloned; otherwise a clear error."""
    local = Path(value).expanduser()
    if local.is_dir():
        return ResolvedRepo(path=local.resolve(), label=local.resolve().name)
    if looks_like_url(value):
        return clone_repo(value)
    raise RepoSourceError(f"--repo is not a directory and not a git URL: {local.resolve()}")
