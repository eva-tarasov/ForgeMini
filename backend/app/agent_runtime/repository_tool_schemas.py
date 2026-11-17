from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.domain.capabilities import Capability
from app.tools.executor import ToolExecutor


READ_TOOL_NAMES = ("list_files", "read_file", "search_files", "git_diff")


def create_repository_tool_model(model: ChatOpenAI, executor: ToolExecutor) -> Runnable:
    """Bind descriptions of registered tools to the model."""
    tools = []
    for name in READ_TOOL_NAMES:
        spec = executor.get_tool_spec(name)
        if spec.capability is not Capability.REPOSITORY_READ or spec.side_effect or spec.requires_approval:
            raise ValueError("repository_model_requires_read_only_tools")
        tools.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema.model_json_schema(),
            },
        })
    return model.bind_tools(tools, parallel_tool_calls=False)
