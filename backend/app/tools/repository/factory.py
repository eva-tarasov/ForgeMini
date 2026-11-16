from functools import partial
from pathlib import Path

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.tools.contracts import ToolRiskLevel, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.registry import RegisteredTool, ToolRegistry
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.filesystem import SafeRepositoryReader
from app.tools.repository.handlers import git_diff, list_files, read_file, search_files
from app.tools.repository.schemas import (
    GitDiffInput,
    GitDiffOutput,
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    SearchFilesInput,
    SearchFilesOutput,
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
            "list_files", "List authorized regular files below a fixture directory.",
            ListFilesInput, ListFilesOutput, list_files, _select_directory,
        ),
        (
            "read_file", "Read one authorized UTF-8 fixture file.",
            ReadFileInput, ReadFileOutput, read_file, _select_path,
        ),
        (
            "search_files", "Find literal text in authorized files below a fixture directory.",
            SearchFilesInput, SearchFilesOutput, search_files, _select_directory,
        ),
        (
            "git_diff", "Compare one authorized file with a trusted fixture revision snapshot.",
            GitDiffInput, GitDiffOutput, partial(git_diff, baseline=baseline), _select_path,
        ),
    )
    tools: list[RegisteredTool] = []
    for name, description, input_schema, output_schema, handler, selector in definitions:
        spec = ToolSpec(
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
        tools.append(RegisteredTool(
            spec=spec,
            handler=partial(handler, reader=reader, spec=spec),
            path_selector=selector,
            bound_context=context,
        ))
    return ToolExecutor(ToolRegistry(tools))
