from functools import partial
from pathlib import Path

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.registry import RegisteredTool, ToolRegistry
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.filesystem import SafeRepositoryReader
from app.tools.repository.handlers import apply_patch, git_diff, list_files, read_file, search_files
from app.tools.repository.patch import parse_patch
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


def _read_spec(
    name: str, description: str, input_schema: type[ToolInput], output_schema: type[ToolOutput],
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version=1,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        capability=Capability.REPOSITORY_READ,
        risk_level=ToolRiskLevel.LOW,
        timeout_ms=2000,
        side_effect=False,
        requires_approval=False,
    )


_READ_FILE_SPEC = _read_spec(
    "read_file", "Read one authorized UTF-8 fixture file.", ReadFileInput, ReadFileOutput,
)


def _select_path(arguments: ReadFileInput | GitDiffInput) -> tuple[str, ...]:
    return (arguments.path,)


def _select_directory(arguments: ListFilesInput | SearchFilesInput) -> tuple[str, ...]:
    return (arguments.directory,)


def create_fixture_executor(
    *, root: Path, context: PermissionContext, baseline: FixtureBaseline
) -> ToolExecutor:
    if type(baseline) is not FixtureBaseline:
        raise TypeError("Fixture tools require a FixtureBaseline")
    reader = SafeRepositoryReader(root, context)
    definitions = (
        (
            _read_spec("list_files", "List authorized regular files below a fixture directory.",
                       ListFilesInput, ListFilesOutput),
            list_files, _select_directory,
        ),
        (_READ_FILE_SPEC, read_file, _select_path),
        (
            _read_spec("search_files", "Find literal text in authorized files below a fixture directory.",
                       SearchFilesInput, SearchFilesOutput),
            search_files, _select_directory,
        ),
        (
            _read_spec("git_diff", "Compare one authorized file with a trusted fixture revision snapshot.",
                       GitDiffInput, GitDiffOutput),
            partial(git_diff, baseline=baseline), _select_path,
        ),
    )
    tools: list[RegisteredTool] = []
    for spec, handler, selector in definitions:
        tools.append(RegisteredTool(
            spec=spec,
            handler=partial(handler, reader=reader, spec=spec),
            path_selector=selector,
            bound_context=context,
        ))
    return ToolExecutor(ToolRegistry(tools))


def _select_patch_paths(arguments: ApplyPatchInput) -> tuple[str, ...]:
    return parse_patch(arguments.patch).paths


def create_fixture_patch_tool(*, root: Path, context: PermissionContext) -> RegisteredTool:
    """Bind a write proposal. The executor's approval gate still blocks its handler."""
    spec = ToolSpec(
        name="apply_patch",
        version=1,
        description="Apply explicit LF unified-diff hunks to existing files in a disposable fixture.",
        input_schema=ApplyPatchInput,
        output_schema=ApplyPatchOutput,
        capability=Capability.REPOSITORY_WRITE,
        risk_level=ToolRiskLevel.HIGH,
        side_effect=True,
        requires_approval=True,
        audit_policy="metadata_only",
        timeout_ms=2000,
    )
    writer = SafeRepositoryWriter(root, context, read_spec=_READ_FILE_SPEC, write_spec=spec)
    return RegisteredTool(
        spec=spec,
        handler=partial(apply_patch, writer=writer),
        path_selector=_select_patch_paths,
        bound_context=context,
    )
