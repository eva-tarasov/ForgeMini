from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.domain.capabilities import Capability
from app.tools.contracts import ToolRiskLevel, ToolSpec
from app.tools.executor import ToolExecutor


READ_TOOL_NAMES = ("list_files", "read_file", "search_files", "git_diff")
PATCH_TOOL_NAME = "apply_patch"


def _read_specs(executor: ToolExecutor) -> list[ToolSpec]:
    specs = []
    for name in READ_TOOL_NAMES:
        spec = executor.get_tool_spec(name)
        if spec.capability is not Capability.REPOSITORY_READ or spec.side_effect or spec.requires_approval:
            raise ValueError("repository_model_requires_read_only_tools")
        specs.append(spec)
    return specs


def _bind_tool_specs(model: ChatOpenAI, specs: list[ToolSpec]) -> Runnable:
    return model.bind_tools([
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema.model_json_schema(),
            },
        }
        for spec in specs
    ], parallel_tool_calls=False)


def create_repository_tool_model(model: ChatOpenAI, executor: ToolExecutor) -> Runnable:
    """Bind descriptions of registered read-only tools to the model."""
    return _bind_tool_specs(model, _read_specs(executor))


def create_approved_repository_tool_model(
    model: ChatOpenAI, read_executor: ToolExecutor, write_executor: ToolExecutor,
) -> Runnable:
    patch = write_executor.get_tool_spec(PATCH_TOOL_NAME)
    if (
        patch.version != 1 or patch.capability is not Capability.REPOSITORY_WRITE
        or patch.risk_level is not ToolRiskLevel.HIGH or not patch.side_effect or not patch.requires_approval
    ):
        raise ValueError("repository_model_requires_approved_patch_tool")
    return _bind_tool_specs(model, [*_read_specs(read_executor), patch])
