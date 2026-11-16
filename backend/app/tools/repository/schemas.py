from pydantic import Field

from app.tools.contracts import ToolInput, ToolOutput


class ReadFileInput(ToolInput):
    path: str


class ReadFileOutput(ToolOutput):
    path: str
    content: str
    size_bytes: int


class ListFilesInput(ToolInput):
    directory: str = "."


class ListFilesOutput(ToolOutput):
    paths: tuple[str, ...]


class SearchFilesInput(ToolInput):
    directory: str = "."
    query: str = Field(min_length=1, max_length=256)


class SearchFilesOutput(ToolOutput):
    matches: tuple[tuple[str, int, str], ...]
    skipped_files: int


class GitDiffInput(ToolInput):
    path: str


class GitDiffOutput(ToolOutput):
    path: str
    revision: str
    diff: str
    changed: bool
