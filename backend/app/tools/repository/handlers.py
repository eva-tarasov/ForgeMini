from difflib import unified_diff
from io import StringIO

from app.tools.contracts import ToolSpec
from app.tools.repository.baseline import FixtureBaseline, diff_lines
from app.tools.repository.filesystem import RepositoryError, SafeRepositoryReader, check_output_size
from app.tools.repository.schemas import (
    ApplyPatchInput,
    ApplyPatchOutput,
    GitDiffInput,
    GitDiffOutput,
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    SearchFilesInput,
    SearchFilesOutput,
)
from app.tools.repository.writer import SafeRepositoryWriter


MAX_SEARCH_MATCHES = 100


async def read_file(
    arguments: ReadFileInput, *, reader: SafeRepositoryReader, spec: ToolSpec
) -> ReadFileOutput:
    content, size = reader.read_text(arguments.path, spec=spec)
    check_output_size(len(arguments.path.encode("utf-8")) + size)
    return ReadFileOutput(path=arguments.path, content=content, size_bytes=size)


async def list_files(
    arguments: ListFilesInput, *, reader: SafeRepositoryReader, spec: ToolSpec
) -> ListFilesOutput:
    return ListFilesOutput(paths=reader.list_files(arguments.directory, spec=spec))


async def search_files(
    arguments: SearchFilesInput, *, reader: SafeRepositoryReader, spec: ToolSpec
) -> SearchFilesOutput:
    matches: list[tuple[str, int, str]] = []
    skipped_files = 0
    payload_size = 0
    with reader.search_texts(arguments.directory, spec=spec) as files:
        for path, text in files:
            if text is None:
                skipped_files += 1
                continue
            path_size = len(path.encode("utf-8"))
            with StringIO(text, newline="") as lines:
                for line_number, line in enumerate(lines, start=1):
                    content = line.rstrip("\r\n")
                    if arguments.query not in content:
                        continue
                    if len(matches) == MAX_SEARCH_MATCHES:
                        raise RepositoryError("repository_search_match_budget_exceeded")
                    payload_size += path_size + len(content.encode("utf-8"))
                    check_output_size(payload_size)
                    matches.append((path, line_number, content))
    matches.sort()
    return SearchFilesOutput(matches=tuple(matches), skipped_files=skipped_files)


async def git_diff(
    arguments: GitDiffInput,
    *,
    reader: SafeRepositoryReader,
    spec: ToolSpec,
    baseline: FixtureBaseline,
) -> GitDiffOutput:
    path = arguments.path
    # The baseline lookup uses the exact same authorized logical path as the read.
    current, _ = reader.read_text(path, spec=spec)
    if path not in baseline.files:
        raise RepositoryError("repository_baseline_entry_missing")
    old_lines = diff_lines(baseline.files[path])
    new_lines = diff_lines(current)
    payload_size = len(path.encode("utf-8")) + len(baseline.revision.encode("utf-8"))
    diff: list[str] = []
    for line in unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"):
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        payload_size += len(line.encode("utf-8"))
        check_output_size(payload_size)
        diff.append(line)
    return GitDiffOutput(path=path, revision=baseline.revision, diff="".join(diff), changed=bool(diff))


async def apply_patch(arguments: ApplyPatchInput, *, writer: SafeRepositoryWriter) -> ApplyPatchOutput:
    paths = writer.apply(arguments.patch)
    return ApplyPatchOutput(changed_paths=paths, changed_files=len(paths))
