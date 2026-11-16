import asyncio
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.tools.contracts import ToolOutput, ToolRiskLevel
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.repository import filesystem as fs
from app.tools.repository.baseline import MAX_DIFF_LINES, FixtureBaseline
from app.tools.repository.factory import create_fixture_executor
from app.tools.repository.handlers import MAX_SEARCH_MATCHES
from app.tools.repository.schemas import GitDiffOutput, ListFilesOutput, ReadFileOutput, SearchFilesOutput


def grants(*scopes: PathScope, run: int = 1, workspace: int = 2) -> PermissionContext:
    return PermissionContext(
        UUID(int=run), UUID(int=workspace), frozenset({Capability.REPOSITORY_READ}), read_scopes=scopes
    )


def fixture_executor(
    root: Path, context: PermissionContext, files: dict[str, str] | None = None
) -> ToolExecutor:
    baseline = FixtureBaseline("fixture-revision", files if files is not None else {})
    return create_fixture_executor(root=root, context=context, baseline=baseline)


def execute(
    executor: ToolExecutor, context: PermissionContext, name: str, /, **arguments: object
) -> ToolOutput:
    return asyncio.run(executor.execute(name, arguments, context=context))


def assert_tool_error(
    executor: ToolExecutor,
    context: PermissionContext,
    name: str,
    code: ToolErrorCode,
    /,
    **arguments: object,
) -> None:
    with pytest.raises(ToolExecutionError, match=f"^{code.value}$") as caught:
        execute(executor, context, name, **arguments)
    assert caught.value.code is code


def test_factory_registers_four_bound_read_only_tools(tmp_path: Path) -> None:
    context = grants(PathScope(".", recursive=True))
    executor = fixture_executor(tmp_path, context)
    registry = executor._registry
    specs = registry.list_specs()

    assert tuple(spec.name for spec in specs) == ("git_diff", "list_files", "read_file", "search_files")
    for spec in specs:
        tool = registry.get(spec.name, version=1)
        assert tool.bound_context is context
        assert tool.synthetic is False
        assert tool.path_selector is not None
        assert spec.capability is Capability.REPOSITORY_READ
        assert spec.risk_level is ToolRiskLevel.LOW
        assert spec.side_effect is False
        assert spec.requires_approval is False
        assert spec.timeout_ms == 2000
        field = "directory" if spec.name in {"list_files", "search_files"} else "path"
        arguments = {field: "src/one.txt"}
        if spec.name == "search_files":
            arguments["query"] = "fixture"
        assert tool.path_selector(spec.input_schema.model_validate(arguments)) == ("src/one.txt",)


def test_factory_keeps_fixture_roots_and_contexts_isolated(tmp_path: Path) -> None:
    roots = (tmp_path / "one", tmp_path / "two")
    contexts = (
        grants(PathScope("same.txt")),
        grants(PathScope("same.txt"), run=3, workspace=4),
    )
    for root in roots:
        root.mkdir()
        (root / "same.txt").write_text(root.name, encoding="utf-8")
    one, two = (fixture_executor(root, context) for root, context in zip(roots, contexts))

    assert execute(one, contexts[0], "read_file", path="same.txt").content == "one"
    assert execute(two, contexts[1], "read_file", path="same.txt").content == "two"
    assert_tool_error(one, contexts[1], "read_file", ToolErrorCode.PERMISSION_DENIED, path="same.txt")
    assert_tool_error(two, contexts[0], "read_file", ToolErrorCode.PERMISSION_DENIED, path="same.txt")
    equal_context = replace(contexts[0])
    assert equal_context == contexts[0] and equal_context is not contexts[0]
    assert_tool_error(one, equal_context, "read_file", ToolErrorCode.PERMISSION_DENIED, path="same.txt")
    with pytest.raises(ToolExecutionError, match="^permission_denied$"):
        asyncio.run(one.execute("read_file", {"path": "same.txt"}))


@pytest.mark.parametrize("extra", ["context", "root", "baseline", "capabilities", "approved", "paths"])
def test_tools_reject_model_supplied_access_configuration(tmp_path: Path, extra: str) -> None:
    context = grants(PathScope("same.txt"))
    (tmp_path / "same.txt").write_text("fixture", encoding="utf-8")
    executor = fixture_executor(tmp_path, context)

    assert_tool_error(executor, context, "read_file", ToolErrorCode.INVALID_INPUT, path="same.txt", **{extra: "."})


def test_read_file_returns_utf8_content_and_byte_count(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    text = "Текст\r\nsecond\nlast"
    data = text.encode("utf-8")
    (tmp_path / "src" / "one.txt").write_bytes(data)
    context = grants(PathScope("src/one.txt"))
    executor = fixture_executor(tmp_path, context)

    output = execute(executor, context, "read_file", path="src/one.txt")
    assert type(output) is ReadFileOutput
    assert output == ReadFileOutput(path="src/one.txt", content=text, size_bytes=len(data))


@pytest.mark.parametrize(
    "path", ["../file.txt", "src//file.txt", "/file.txt", ".git/config", "src/.eNv.local", "outside.txt"]
)
def test_read_file_denies_malformed_protected_and_out_of_scope_paths(tmp_path: Path, path: str) -> None:
    context = grants(PathScope("src", recursive=True), PathScope(".git", recursive=True))
    assert_tool_error(fixture_executor(tmp_path, context), context, "read_file", ToolErrorCode.PERMISSION_DENIED, path=path)


def test_read_file_denies_missing_grant_and_rejects_missing_file(tmp_path: Path) -> None:
    context = replace(grants(PathScope(".", recursive=True)), capabilities=frozenset())
    assert_tool_error(
        fixture_executor(tmp_path, context), context, "read_file", ToolErrorCode.PERMISSION_DENIED, path="absent"
    )
    context = grants(PathScope("absent"))
    assert_tool_error(
        fixture_executor(tmp_path, context), context, "read_file", ToolErrorCode.HANDLER_FAILED, path="absent"
    )


def test_list_files_recursively_returns_sorted_authorized_regular_paths(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "z.txt").touch()
    (source / "sub" / "a.txt").touch()
    (source / "sub.txt").touch()
    (source / ".GIT").mkdir()
    (source / ".GIT" / "marker").touch()
    (source / ".EnV.test").touch()
    (source / "link").symlink_to(source / "z.txt")
    os.mkfifo(source / "pipe")
    (tmp_path / "sibling.txt").touch()
    context = grants(PathScope("."), PathScope("src", recursive=True))
    executor = fixture_executor(tmp_path, context)

    output = execute(executor, context, "list_files")
    assert type(output) is ListFilesOutput
    assert output.paths == ("src/sub.txt", "src/sub/a.txt", "src/z.txt")
    assert_tool_error(executor, context, "list_files", ToolErrorCode.PERMISSION_DENIED, directory=".GIT")


def test_list_files_exact_directory_does_not_grant_descendants(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file.txt").touch()
    (source / "neighbor.txt").touch()
    context = grants(PathScope("src"), PathScope("src/nested/file.txt"))
    executor = fixture_executor(tmp_path, context)

    assert execute(executor, context, "list_files", directory="src").paths == ()
    assert execute(executor, context, "read_file", path="src/nested/file.txt").content == ""


def test_search_files_returns_literal_ordered_matches_and_skipped_count(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "b.txt").write_bytes(b"plain\r\nneedle. here\r\nneedleX")
    (source / "sub.txt").write_bytes(b"Needle.\nneedle.\nlast needle.")
    (source / "binary.bin").write_bytes(b"needle.\0")
    (source / "invalid.txt").write_bytes(b"\xff")
    (source / "large.txt").write_bytes(b"x" * (fs.MAX_FILE_BYTES + 1))
    (source / ".ENV.test").write_bytes(b"needle.\0")
    (tmp_path / "hidden.txt").write_bytes(b"needle.\0")
    context = grants(PathScope("."), PathScope("src", recursive=True))
    executor = fixture_executor(tmp_path, context)

    output = execute(executor, context, "search_files", query="needle.")
    assert type(output) is SearchFilesOutput
    assert output.matches == (
        ("src/sub.txt", 2, "needle."),
        ("src/sub.txt", 3, "last needle."),
        ("src/sub/b.txt", 2, "needle. here"),
    )
    assert output.skipped_files == 3


@pytest.mark.parametrize("query", ["", "x" * 257])
def test_search_rejects_out_of_range_query(tmp_path: Path, query: str) -> None:
    context = grants(PathScope(".", recursive=True))
    assert_tool_error(fixture_executor(tmp_path, context), context, "search_files", ToolErrorCode.INVALID_INPUT, query=query)


@pytest.mark.parametrize("budget", ["candidates", "bytes", "matches", "output"])
def test_search_fails_on_hard_budget_exhaustion(tmp_path: Path, budget: str) -> None:
    context = grants(PathScope(".", recursive=True))
    if budget == "candidates":
        for index in range(fs.MAX_SEARCH_FILES + 1):
            (tmp_path / f"file-{index:03}").touch()
    elif budget == "bytes":
        for index in range(5):
            (tmp_path / f"file-{index}").write_bytes(b"\xff" * (60 * 1024))
    elif budget == "matches":
        (tmp_path / "file").write_bytes(b"needle\n" * (MAX_SEARCH_MATCHES + 1))
    else:
        for index in range(2):
            (tmp_path / f"file-{index}").write_bytes(b"needle" + b"x" * (35 * 1024))

    assert_tool_error(fixture_executor(tmp_path, context), context, "search_files", ToolErrorCode.HANDLER_FAILED, query="needle")


def test_list_and_read_reject_exceeded_output_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = grants(PathScope(".", recursive=True))
    (tmp_path / "long-name-one").write_bytes(b"x" * fs.MAX_FILE_BYTES)
    (tmp_path / "long-name-two").touch()
    executor = fixture_executor(tmp_path, context)
    assert_tool_error(executor, context, "read_file", ToolErrorCode.HANDLER_FAILED, path="long-name-one")

    monkeypatch.setattr(fs, "MAX_OUTPUT_BYTES", 20)
    assert_tool_error(executor, context, "list_files", ToolErrorCode.HANDLER_FAILED)


def test_git_diff_compares_against_actual_temporary_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git executable unavailable for temporary commit fixture")
    root = tmp_path / "git-fixture"
    root.mkdir()
    config = tmp_path / "git-test-config"
    config.mkdir()
    environment = {
        "PATH": os.defpath,
        "HOME": str(config),
        "XDG_CONFIG_HOME": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Fixture Test",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Fixture Test",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
        "LC_ALL": "C",
    }

    def git_fixture(*arguments: str) -> bytes:
        return subprocess.run(
            [git, *arguments], cwd=root, env=environment, shell=False, check=True,
            capture_output=True, timeout=10,
        ).stdout

    git_fixture("init", "--quiet", "--template=")
    fixture = root / "fixture.txt"
    fixture.write_bytes(b"one\nold\n")
    git_fixture("add", "--", "fixture.txt")
    git_fixture("commit", "--quiet", "-m", "Synthetic fixture baseline")
    revision = git_fixture("rev-parse", "HEAD").decode("ascii").strip()
    committed = git_fixture("show", "HEAD:fixture.txt").decode("utf-8")
    assert committed == "one\nold\n"
    baseline = FixtureBaseline(revision, {"fixture.txt": committed})
    fixture.write_bytes(b"one\nnew\n")
    context = grants(PathScope("fixture.txt"))
    executor = create_fixture_executor(root=root, context=context, baseline=baseline)

    def forbidden_subprocess(*_args, **_kwargs):
        pytest.fail("The fixture tool must not run Git")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    output = execute(executor, context, "git_diff", path="fixture.txt")
    assert type(output) is GitDiffOutput
    assert output == GitDiffOutput(
        path="fixture.txt", revision=revision, changed=True,
        diff="--- a/fixture.txt\n+++ b/fixture.txt\n@@ -1,2 +1,2 @@\n one\n-old\n+new\n",
    )


def test_git_diff_preserves_line_endings_handles_missing_newline_and_unchanged_files(tmp_path: Path) -> None:
    fixture = tmp_path / "file.txt"
    fixture.write_bytes(b"keep\r\nold")
    files = {"file.txt": "keep\r\nold"}
    baseline = FixtureBaseline("fixture-revision", files)
    files["file.txt"] = "modified outside snapshot"
    with pytest.raises(TypeError):
        baseline.files["file.txt"] = "also modified"
    context = grants(PathScope("file.txt"))
    executor = create_fixture_executor(root=tmp_path, context=context, baseline=baseline)

    unchanged = execute(executor, context, "git_diff", path="file.txt")
    assert unchanged.diff == "" and unchanged.changed is False
    fixture.write_bytes(b"keep\r\nnew")
    changed = execute(executor, context, "git_diff", path="file.txt")
    assert changed.changed is True
    assert changed.diff == (
        "--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1,2 @@\n keep\r\n"
        "-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    )


def test_git_diff_marks_a_trailing_cr_without_lf_as_unterminated(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"new\n")
    context = grants(PathScope("file.txt"))
    executor = fixture_executor(tmp_path, context, {"file.txt": "old\r"})

    output = execute(executor, context, "git_diff", path="file.txt")

    assert output.diff.endswith("-old\r\n\\ No newline at end of file\n+new\n")


def test_git_diff_rejects_missing_or_out_of_scope_entries(tmp_path: Path) -> None:
    (tmp_path / "current-only.txt").write_bytes(b"one\n")
    (tmp_path / "private.txt").write_bytes(b"synthetic private content")
    (tmp_path / ".env.test").write_bytes(b"synthetic protected content")
    context = grants(PathScope("current-only.txt"), PathScope("baseline-only.txt"), PathScope(".env.test"))
    executor = fixture_executor(tmp_path, context, {"baseline-only.txt": "old", "private.txt": "synthetic baseline"})
    for path in ("current-only.txt", "baseline-only.txt"):
        assert_tool_error(executor, context, "git_diff", ToolErrorCode.HANDLER_FAILED, path=path)
    for path in ("private.txt", ".env.test"):
        assert_tool_error(executor, context, "git_diff", ToolErrorCode.PERMISSION_DENIED, path=path)


def test_git_diff_rejects_symlink_and_hard_link_current_file(tmp_path: Path) -> None:
    current = tmp_path / "original"
    current.write_bytes(b"synthetic current\n")
    (tmp_path / "link").symlink_to(current)
    os.link(current, tmp_path / "hard")
    context = grants(PathScope(".", recursive=True))
    executor = fixture_executor(tmp_path, context, {"link": "old", "hard": "old"})
    for path in ("link", "hard"):
        assert_tool_error(executor, context, "git_diff", ToolErrorCode.HANDLER_FAILED, path=path)


@pytest.mark.parametrize("budget", ["lines", "output"])
def test_git_diff_rejects_line_and_output_budget_exhaustion(tmp_path: Path, budget: str) -> None:
    path = tmp_path / "file.txt"
    if budget == "lines":
        path.write_bytes(b"x\n" * (MAX_DIFF_LINES + 1))
        old = "old"
    else:
        path.write_bytes(b"x" * (40 * 1024))
        old = "y" * (40 * 1024)
    context = grants(PathScope("file.txt"))
    executor = fixture_executor(tmp_path, context, {"file.txt": old})

    assert_tool_error(executor, context, "git_diff", ToolErrorCode.HANDLER_FAILED, path="file.txt")


@pytest.mark.parametrize(
    "files",
    [
        {"../file.txt": "old"},
        {"src/.GiT/config": "old"},
        {".EnV.test": "old"},
        {".": "old"},
        {"file.txt": "x\0"},
        {"file.txt": "x" * (fs.MAX_FILE_BYTES + 1)},
        {"file.txt": "x\n" * (MAX_DIFF_LINES + 1)},
        {f"file-{index}": "x" * (60 * 1024) for index in range(5)},
    ],
    ids=["traversal", "git", "env", "root", "nul", "file-bytes", "lines", "total-bytes"],
)
def test_baseline_rejects_unfit_snapshot_entries(files: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        FixtureBaseline("fixture-revision", files)
