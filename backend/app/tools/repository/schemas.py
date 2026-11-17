from pydantic import Field, field_validator

from app.tools.contracts import ToolInput, ToolOutput
from app.tools.repository.patch import MAX_PATCH_BYTES, parse_patch


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


class ApplyPatchInput(ToolInput):
    patch: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)

    @field_validator("patch")
    @classmethod
    def validate_patch(cls, text: str) -> str:
        parse_patch(text)
        return text


class ApplyPatchOutput(ToolOutput):
    changed_paths: tuple[str, ...]
    changed_files: int
