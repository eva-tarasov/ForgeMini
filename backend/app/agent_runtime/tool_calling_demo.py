from collections.abc import Sequence
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import tool_call
from langchain_core.runnables import Runnable

from app.agent_runtime.fixture_tools import (
    FixtureInput as FixtureInput,
    create_tool_calling_model as create_tool_calling_model,
    read_fixture as read_fixture,
)
from app.agent_runtime.model import build_task_messages


_SYSTEM_INSTRUCTIONS = (
    "Analyze the supplied task and acceptance criteria. "
    "Use read_fixture only to consult predefined training fixtures. "
    "Distinguish synthetic fixture data from your assumptions. "
    "Do not claim you read real files, changed code, or ran tests."
)


class ToolCallingDemoError(RuntimeError):
    """The educational tool flow received an invalid response or exceeded its budget."""


def _checked_ai_message(message: object) -> AIMessage:
    if not isinstance(message, AIMessage):
        raise ToolCallingDemoError("invalid_model_message")
    if message.invalid_tool_calls:
        raise ToolCallingDemoError("invalid_tool_call")
    return message


async def run_tool_calling_demo(
    model: Runnable, *, goal: str, acceptance_criteria: Sequence[str]
) -> list[BaseMessage]:
    """Return the transcript of up to two model calls and one fixture execution."""
    messages = build_task_messages(goal, acceptance_criteria)
    messages[0] = SystemMessage(content=_SYSTEM_INSTRUCTIONS)

    first = _checked_ai_message(await model.ainvoke(messages))
    messages.append(first)
    if not first.tool_calls:
        return messages
    if len(first.tool_calls) > 1:
        raise ToolCallingDemoError("tool_budget_exceeded")

    request = first.tool_calls[0]
    if request["name"] != read_fixture.name:
        raise ToolCallingDemoError("unknown_tool")
    call_id = request.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ToolCallingDemoError("invalid_tool_call_id")

    fixture = await read_fixture.ainvoke(
        tool_call(name=request["name"], args=request["args"], id=call_id)
    )
    if not isinstance(fixture, ToolMessage):
        raise ToolCallingDemoError("invalid_tool_message")
    if fixture.tool_call_id != call_id:
        raise ToolCallingDemoError("tool_call_id_mismatch")
    if fixture.status != "success":
        raise ToolCallingDemoError("tool_execution_failed")
    messages.append(fixture)

    final = _checked_ai_message(await model.ainvoke(messages))
    if final.tool_calls:
        raise ToolCallingDemoError("tool_budget_exceeded")
    messages.append(final)
    return messages
