from collections.abc import Mapping, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable

from app.agent_runtime.model import build_task_messages
from app.agent_runtime.tool_calling_demo import read_fixture


_SYSTEM_PROMPT = (
    "Analyze the supplied task goal and acceptance criteria. "
    "Use read_fixture only to consult predefined synthetic training fixtures. "
    "Distinguish fixture data from your assumptions. "
    "Do not claim you read real files, changed code, or ran tests."
)


class CreateAgentDemoError(RuntimeError):
    """The educational agent returned an invalid or unfinished history."""


def create_fixture_agent(model: BaseChatModel) -> Runnable:
    return create_agent(
        model=model,
        tools=[read_fixture],
        system_prompt=_SYSTEM_PROMPT,
        middleware=[
            ModelCallLimitMiddleware(run_limit=2, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=1, exit_behavior="error"),
        ],
        checkpointer=None,
    )


async def run_create_agent_demo(
    agent: Runnable, *, goal: str, acceptance_criteria: Sequence[str]
) -> list[BaseMessage]:
    """Return the agent history for learning and diagnostics."""
    human_messages = [
        message
        for message in build_task_messages(goal, acceptance_criteria)
        if isinstance(message, HumanMessage)
    ]
    state = await agent.ainvoke({"messages": human_messages}, config={"recursion_limit": 16})
    if not isinstance(state, Mapping):
        raise CreateAgentDemoError("invalid_state")

    messages = state.get("messages")
    if not isinstance(messages, list) or not messages or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise CreateAgentDemoError("invalid_messages")

    final = messages[-1]
    if not isinstance(final, AIMessage):
        raise CreateAgentDemoError("invalid_final_message")
    if final.tool_calls or final.invalid_tool_calls:
        raise CreateAgentDemoError("unfinished_tool_request")
    return messages
