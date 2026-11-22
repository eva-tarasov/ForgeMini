"""`forge solve --repo <git URL>`: a URL is untrusted input, so these tests are mostly about what is REFUSED."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forge_cli import repo_source
from forge_cli.repo_source import (
    RepoSourceError, clone_destination, clone_repo, looks_like_url, resolve_repo, validate_git_url,
)

GOOD = [
    "https://github.com/eva-tarasov/forge-demo-shop.git",
    "https://github.com/eva-tarasov/forge-demo-shop",
    "git@github.com:eva-tarasov/forge-demo-shop.git",
    "ssh://git@github.com/eva-tarasov/forge-demo-shop.git",
    "git://example.org/repo.git",
    "file:///tmp/some/repo",
]

BAD = [
    ("--upload-pack=touch /tmp/pwned", "start with '-'"),                   # option injection
    ("-oProxyCommand=id", "start with '-'"),
    ("ext::sh -c id% /tmp/x", "spaces or control"),                         # command-running transport
    ("ext::sh,-c,id", "Unsupported"),
    ("https://user:s3cret@github.com/o/r.git", "credentials"),
    ("https://tokenonly@github.com/o/r.git", "credentials"),
    ("ssh://git:pw@github.com/o/r.git", "credentials"),
    ("http://github.com/o/r.git", "https://"),
    ("ftp://example.org/r.git", "Unsupported"),
    ("https://github.com/o r.git", "spaces or control"),
    ("https://github.com/o/r.git\n--upload-pack=x", "spaces or control"),
    ("https:///o/r.git", "no host"),
    ("file://", "no path"),
]


@pytest.mark.parametrize("url", GOOD)
def test_ordinary_urls_are_accepted(url):
    assert validate_git_url(url) == url and looks_like_url(url)


@pytest.mark.parametrize(("url", "why"), BAD)
def test_dangerous_or_malformed_urls_are_refused(url, why):
    with pytest.raises(RepoSourceError, match=why):
        validate_git_url(url)


def test_a_refused_value_never_reaches_git(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git must not be started for a refused URL")

    monkeypatch.setattr(subprocess, "run", boom)
    for value, _ in BAD:
        with pytest.raises(RepoSourceError):
            resolve_repo(value)
    with pytest.raises(RepoSourceError, match="not a directory"):
        resolve_repo("--upload-pack=x")                 # not a URL either -> plain "not a directory"


def test_git_is_called_with_double_dash_prompts_off_and_protocol_allowlist(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.setdefault("calls", []).append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("FORGE_CLONES_DIR", str(tmp_path))
    res = clone_repo("https://github.com/o/r.git")
    argv, kw = seen["calls"][0]
    assert argv[:2] == ["git", "clone"] and "--depth" in argv
    assert argv[argv.index("--") + 1] == "https://github.com/o/r.git"    # URL only AFTER "--"
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert set(kw["env"]["GIT_ALLOW_PROTOCOL"].split(":")) == {"https", "ssh", "git", "file"}
    assert kw["timeout"] == repo_source.CLONE_TIMEOUT_SECONDS
    assert res.commit == "abc1234" and res.cloned_from == "https://github.com/o/r.git"


def test_destination_is_unique_per_url_and_per_moment(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CLONES_DIR", str(tmp_path))
    t1, t2 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC), datetime(2026, 11, 1, 10, 0, 1, tzinfo=UTC)
    a1 = clone_destination("https://github.com/o/shop.git", t1)
    a2 = clone_destination("https://github.com/o/shop.git", t2)
    b1 = clone_destination("https://github.com/x/shop.git", t1)
    assert len({a1, a2, b1}) == 3 and a1.parent == tmp_path
    assert a1.name.startswith("shop-") and a1.name.endswith("-20261101T100000Z")
    weird = clone_destination("https://github.com/o/we ird$(id).git".replace(" ", ""), t1)
    assert "$" not in weird.name and "(" not in weird.name


def _make_repo(path: Path) -> str:
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (path / "a.txt").write_text("hello\n")
    run("add", ".")
    run("commit", "-q", "-m", "first")
    return run("rev-parse", "--short", "HEAD").stdout.strip()


def test_real_clone_from_a_file_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_CLONES_DIR", str(tmp_path / "clones"))
    sha = _make_repo(tmp_path / "origin")
    res = resolve_repo((tmp_path / "origin").as_uri())
    assert (res.path / "a.txt").read_text() == "hello\n" and res.commit == sha
    assert res.path.parent == (tmp_path / "clones").resolve() and res.cloned_from


def test_failed_clone_explains_and_mentions_private_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_CLONES_DIR", str(tmp_path / "clones"))
    with pytest.raises(RepoSourceError, match="(?s)git clone failed.*private repository"):
        clone_repo((tmp_path / "does-not-exist").as_uri())


def test_missing_git_binary_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CLONES_DIR", str(tmp_path))

    def nogit(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", nogit)
    with pytest.raises(RepoSourceError, match="git is not installed"):
        clone_repo("https://github.com/o/r.git")


def test_existing_local_directory_wins_over_url_detection(tmp_path):
    res = resolve_repo(str(tmp_path))
    assert res.path == tmp_path.resolve() and res.cloned_from is None
