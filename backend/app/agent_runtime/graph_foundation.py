from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph


class TaskPreviewInput(TypedDict):
    goal: str
    acceptance_criteria: list[str]


class TaskPreviewState(TaskPreviewInput):
    """Internal state for the task preview exercise."""

    normalized_goal: NotRequired[str]
    outcome: NotRequired[Literal["prepared", "blocked"]]
    preview: NotRequired[str]
    blocked_reason: NotRequired[str]


def normalize_goal(state: TaskPreviewState) -> dict[str, str]:
    return {"normalized_goal": state["goal"].strip()}


def prepare_preview(state: TaskPreviewState) -> dict[str, str]:
    return {"outcome": "prepared", "preview": f"Goal: {state['normalized_goal']}"}


def block_preview(state: TaskPreviewState) -> dict[str, str]:
    return {"outcome": "blocked", "blocked_reason": "empty_goal"}


def route_preview(state: TaskPreviewState) -> Literal["ready", "blocked"]:
    return "ready" if state["normalized_goal"] else "blocked"


def create_task_preview_graph() -> CompiledStateGraph:
    builder = StateGraph(TaskPreviewState, input_schema=TaskPreviewInput)
    builder.add_node("normalize_goal", normalize_goal)
    builder.add_node("prepare_preview", prepare_preview)
    builder.add_node("block_preview", block_preview)
    builder.add_edge(START, "normalize_goal")
    builder.add_conditional_edges(
        "normalize_goal",
        route_preview,
        {"ready": "prepare_preview", "blocked": "block_preview"},
    )
    builder.add_edge("prepare_preview", END)
    builder.add_edge("block_preview", END)
    return builder.compile(checkpointer=False)
