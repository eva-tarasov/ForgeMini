import operator
from collections.abc import Sequence
from typing import Annotated, Literal, TypeVar, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import tool_call
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from app.agent_runtime.fixture_tools import FixtureInput, read_fixture
from app.agent_runtime.model import build_task_messages


MAX_MODEL_CALLS = 2
MAX_TOOL_CALLS = 1
GRAPH_RECURSION_LIMIT = 16

_SYSTEM_INSTRUCTIONS = (
    "Analyze the supplied goal and acceptance criteria. "
    "Use read_fixture only for predefined synthetic training data. "
    "Distinguish fixture data from assumptions and state which conclusions remain unverified. "
    "Do not claim you read real files, changed code, or ran tests."
)


class AgentGraphInput(TypedDict):
    goal: str
    acceptance_criteria: list[str]


class AgentGraphState(AgentGraphInput):
    """Internal execution state for a single graph invocation."""

    messages: Annotated[list[AnyMessage], add_messages]
    model_calls: Annotated[int, operator.add]
    tool_calls: Annotated[int, operator.add]


class AgentGraphError(RuntimeError):
    """The graph received an invalid response or hit an execution boundary."""


_ResponseMessage = TypeVar("_ResponseMessage", AIMessage, ToolMessage)


def _prepare_message(
    message: _ResponseMessage, history: Sequence[AnyMessage]
) -> _ResponseMessage:
    message_id = message.id
    if message_id is None:
        message_id = str(uuid4())
        message = message.model_copy(update={"id": message_id})
    elif not message_id.strip():
        raise AgentGraphError("invalid_message_id")
    if any(previous.id == message_id for previous in history):
        raise AgentGraphError("duplicate_message_id")
    return message


def initialize(state: AgentGraphInput) -> dict[str, list[AnyMessage] | int]:
    messages = build_task_messages(state["goal"], state["acceptance_criteria"])
    messages[0] = SystemMessage(content=_SYSTEM_INSTRUCTIONS)
    return {"messages": messages, "model_calls": 0, "tool_calls": 0}


async def call_model(state: AgentGraphState, tool_model: Runnable) -> dict[str, list[AnyMessage] | int]:
    if state["model_calls"] >= MAX_MODEL_CALLS:
        raise AgentGraphError("model_budget_exceeded")

    response = await tool_model.ainvoke(state["messages"])
    if not isinstance(response, AIMessage):
        raise AgentGraphError("invalid_model_message")
    if response.invalid_tool_calls:
        raise AgentGraphError("invalid_tool_call")
    if response.additional_kwargs.get("refusal"):
        raise AgentGraphError("model_refusal")
    if response.response_metadata.get("finish_reason") in ("length", "content_filter"):
        raise AgentGraphError("incomplete_model_response")
    if not response.tool_calls and not response.text.strip():
        raise AgentGraphError("empty_model_response")

    return {"messages": [_prepare_message(response, state["messages"])], "model_calls": 1}


def route_after_model(state: AgentGraphState) -> Literal["tool", "finish"]:
    last = state["messages"][-1]
    return "tool" if isinstance(last, AIMessage) and last.tool_calls else "finish"


async def call_tool(state: AgentGraphState) -> dict[str, list[AnyMessage] | int]:
    history = state["messages"]
    if not history or not isinstance(history[-1], AIMessage):
        raise AgentGraphError("invalid_tool_request")
    if state["tool_calls"] >= MAX_TOOL_CALLS:
        raise AgentGraphError("tool_budget_exceeded")
    if state["model_calls"] >= MAX_MODEL_CALLS:
        raise AgentGraphError("model_budget_exceeded")

    requests = history[-1].tool_calls
    if len(requests) != 1:
        raise AgentGraphError("invalid_tool_call_count")
    request = requests[0]
    if request["name"] != read_fixture.name:
        raise AgentGraphError("unknown_tool")
    call_id = request.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        raise AgentGraphError("invalid_tool_call_id")
    try:
        args = FixtureInput.model_validate(request["args"])
    except ValidationError:
        raise AgentGraphError("invalid_tool_arguments") from None

    observation = await read_fixture.ainvoke(
        tool_call(name=read_fixture.name, args=args.model_dump(), id=call_id)
    )
    if not isinstance(observation, ToolMessage):
        raise AgentGraphError("invalid_tool_message")
    if observation.tool_call_id != call_id:
        raise AgentGraphError("tool_call_id_mismatch")
    if observation.status != "success":
        raise AgentGraphError("tool_execution_failed")

    return {"messages": [_prepare_message(observation, history)], "tool_calls": 1}


def create_agent_graph(tool_model: Runnable) -> CompiledStateGraph:
    async def model_node(state: AgentGraphState) -> dict[str, list[AnyMessage] | int]:
        return await call_model(state, tool_model)

    builder = StateGraph(AgentGraphState, input_schema=AgentGraphInput)
    builder.add_node("initialize", initialize)
    builder.add_node("model", model_node)
    builder.add_node("tool", call_tool)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "model")
    builder.add_conditional_edges("model", route_after_model, {"tool": "tool", "finish": END})
    builder.add_edge("tool", "model")
    return builder.compile(checkpointer=False)


async def run_agent_graph(
    graph: CompiledStateGraph, *, goal: str, acceptance_criteria: Sequence[str]
) -> AgentGraphState:
    return await graph.ainvoke(
        {"goal": goal, "acceptance_criteria": list(acceptance_criteria)},
        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
    )
