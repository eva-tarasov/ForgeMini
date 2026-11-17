"""Pure parser and exact LF text application for a restricted unified diff subset."""

import re
from dataclasses import dataclass

from app.permissions.paths import has_protected_component, parse_logical_path
from app.tools.repository.filesystem import MAX_FILE_BYTES, MAX_PATH_DEPTH


MAX_PATCH_BYTES = 64 * 1024
MAX_PATCH_FILES = 4
MAX_PATCH_HUNKS = 32
MAX_FILE_LINES = 1000
PATCH_TEMP_PREFIX = ".forge-patch-"
_RANGE = re.compile(r"@@ -([1-9][0-9]{0,3}),([1-9][0-9]{0,3}) \+([1-9][0-9]{0,3}),([1-9][0-9]{0,3}) @@")


class PatchTextError(ValueError):
    """A patch or text rejection with a fixed diagnostic code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HunkLine:
    prefix: str
    text: str


@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]


@dataclass(frozen=True, slots=True)
class FilePatch:
    path: str
    components: tuple[str, ...]
    hunks: tuple[PatchHunk, ...]


@dataclass(frozen=True, slots=True)
class ParsedPatch:
    files: tuple[FilePatch, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)


def _check_text(text: str, byte_limit: int, invalid_code: str) -> None:
    if not isinstance(text, str):
        raise PatchTextError(invalid_code)
    if len(text) > byte_limit:
        raise PatchTextError("patch_limit_exceeded")
    if not text.endswith("\n") or "\r" in text or "\0" in text:
        raise PatchTextError(invalid_code)
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise PatchTextError(invalid_code) from None
    if size > byte_limit:
        raise PatchTextError("patch_limit_exceeded")


def _file_header(old: str, new: str) -> tuple[str, tuple[str, ...]]:
    if not old.startswith("--- a/") or not new.startswith("+++ b/") or old[6:] != new[6:]:
        raise PatchTextError("invalid_patch")
    path = old[6:]
    try:
        components = parse_logical_path(path)
    except ValueError:
        raise PatchTextError("invalid_patch_path") from None
    if (
        not components or len(components) > MAX_PATH_DEPTH
        or any(character.isspace() for character in path)
        or has_protected_component(components)
        or any(component.casefold().startswith(PATCH_TEMP_PREFIX) for component in components)
    ):
        raise PatchTextError("invalid_patch_path")
    return path, components


def _parse_hunk(lines: list[str], index: int) -> tuple[PatchHunk, int]:
    header = _RANGE.fullmatch(lines[index])
    if header is None:
        raise PatchTextError("invalid_patch_hunk")
    old_start, old_count, new_start, new_count = map(int, header.groups())
    if max(old_start + old_count - 1, new_start + new_count - 1) > MAX_FILE_LINES:
        raise PatchTextError("patch_limit_exceeded")
    old_remaining, new_remaining = old_count, new_count
    body: list[HunkLine] = []
    changed = False
    index += 1
    while old_remaining or new_remaining:
        if index == len(lines) or not lines[index] or lines[index][0] not in " -+":
            raise PatchTextError("invalid_patch_hunk")
        prefix, content = lines[index][0], lines[index][1:]
        old_remaining -= prefix != "+"
        new_remaining -= prefix != "-"
        if old_remaining < 0 or new_remaining < 0:
            raise PatchTextError("invalid_patch_hunk")
        changed |= prefix != " "
        body.append(HunkLine(prefix, content))
        index += 1
    if not changed:
        raise PatchTextError("invalid_patch_hunk")
    return PatchHunk(old_start, old_count, new_start, new_count, tuple(body)), index


def parse_patch(text: str) -> ParsedPatch:
    """Accept only explicit existing-file hunks. Git headers and newline markers are outside v1."""
    _check_text(text, MAX_PATCH_BYTES, "invalid_patch")
    lines = text[:-1].split("\n")
    files: list[FilePatch] = []
    paths: set[str] = set()
    index = hunk_count = 0
    while index < len(lines):
        if len(files) == MAX_PATCH_FILES:
            raise PatchTextError("patch_limit_exceeded")
        if index + 1 >= len(lines):
            raise PatchTextError("invalid_patch")
        path, components = _file_header(lines[index], lines[index + 1])
        if path in paths:
            raise PatchTextError("duplicate_patch_path")
        paths.add(path)
        index += 2
        hunks: list[PatchHunk] = []
        old_end = new_end = 0  # Zero-based exclusive positions.
        while index < len(lines) and lines[index].startswith("@@"):
            if hunk_count == MAX_PATCH_HUNKS:
                raise PatchTextError("patch_limit_exceeded")
            hunk, index = _parse_hunk(lines, index)
            old_gap = hunk.old_start - 1 - old_end
            new_gap = hunk.new_start - 1 - new_end
            if old_gap < 0 or new_gap != old_gap:
                raise PatchTextError("invalid_patch_coordinates")
            old_end = hunk.old_start - 1 + hunk.old_count
            new_end = hunk.new_start - 1 + hunk.new_count
            hunks.append(hunk)
            hunk_count += 1
        if not hunks:
            raise PatchTextError("invalid_patch")
        if all(
            tuple(line.text for line in hunk.lines if line.prefix != "+")
            == tuple(line.text for line in hunk.lines if line.prefix != "-")
            for hunk in hunks
        ):
            raise PatchTextError("patch_no_change")
        files.append(FilePatch(path, components, tuple(hunks)))
    return ParsedPatch(tuple(files))


def apply_file_patch(original: str, patch: FilePatch) -> str:
    _check_text(original, MAX_FILE_BYTES, "patch_unsupported_text")
    if original.count("\n") > MAX_FILE_LINES:
        raise PatchTextError("patch_limit_exceeded")
    source = original[:-1].split("\n")
    result: list[str] = []
    cursor = 0
    for hunk in patch.hunks:
        old, new = hunk.old_start - 1, hunk.new_start - 1
        if not cursor <= old < len(source) or old + hunk.old_count > len(source):
            raise PatchTextError("patch_conflict")
        result.extend(source[cursor:old])
        if len(result) != new:
            raise PatchTextError("invalid_patch_coordinates")
        cursor = old
        for line in hunk.lines:
            if line.prefix != "+":
                if cursor >= len(source) or source[cursor] != line.text:
                    raise PatchTextError("patch_conflict")
                cursor += 1
            if line.prefix != "-":
                result.append(line.text)
        if cursor != old + hunk.old_count or len(result) != new + hunk.new_count:
            raise PatchTextError("invalid_patch_hunk")
    result.extend(source[cursor:])
    if not result or len(result) > MAX_FILE_LINES:
        raise PatchTextError("patch_limit_exceeded")
    updated = "\n".join(result) + "\n"
    _check_text(updated, MAX_FILE_BYTES, "patch_unsupported_text")
    if updated == original:
        raise PatchTextError("patch_no_change")
    return updated
