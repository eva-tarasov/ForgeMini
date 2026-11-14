from typing import Literal

from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict


class FixtureInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    component: Literal["task_service", "agent_run_service"]


_FIXTURES = {
    "task_service": (
        "Training fixture (synthetic): an idempotency key is scoped to its workspace. "
        "The same key may be used in a different workspace."
    ),
    "agent_run_service": (
        "Training fixture (synthetic): a transition checks the expected current status "
        "against the stored status before changing it."
    ),
}


@tool(args_schema=FixtureInput)
async def read_fixture(component: Literal["task_service", "agent_run_service"]) -> str:
    """Return a predefined synthetic training fixture for the selected component."""
    return _FIXTURES[component]


def create_tool_calling_model(model: ChatOpenAI) -> Runnable:
    return model.bind_tools([read_fixture], parallel_tool_calls=False)
