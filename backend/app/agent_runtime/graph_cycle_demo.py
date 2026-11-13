import operator
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph


class DraftCycleInput(TypedDict):
    topic: str


class DraftCycleState(DraftCycleInput):
    """Internal execution state for the draft cycle exercise."""

    messages: Annotated[list[AnyMessage], add_messages]
    rounds: Annotated[int, operator.add]
    outcome: NotRequired[Literal["prepared"]]


def initialize(state: DraftCycleInput) -> dict[str, list[AnyMessage] | int]:
    return {"messages": [HumanMessage(id="demo-user", content=state["topic"])], "rounds": 0}


def draft_round(state: DraftCycleState) -> dict[str, list[AnyMessage] | int]:
    round_number = state["rounds"] + 1
    return {
        "rounds": 1,
        "messages": [
            AIMessage(
                id=f"demo-draft-{round_number}",
                content=f"Training draft {round_number}: {state['topic']}",
            )
        ],
    }


def finalize(state: DraftCycleState) -> dict[str, list[AnyMessage] | Literal["prepared"]]:
    return {
        "messages": [
            AIMessage(
                id=f"demo-draft-{state['rounds']}",
                content=f"Prepared training preview: {state['topic']}",
            )
        ],
        "outcome": "prepared",
    }


def route_round(state: DraftCycleState) -> Literal["continue", "finish"]:
    return "continue" if state["rounds"] < 2 else "finish"


def create_draft_cycle_graph() -> CompiledStateGraph:
    builder = StateGraph(DraftCycleState, input_schema=DraftCycleInput)
    builder.add_node("initialize", initialize)
    builder.add_node("draft_round", draft_round)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "draft_round")
    builder.add_conditional_edges(
        "draft_round",
        route_round,
        {"continue": "draft_round", "finish": "finalize"},
    )
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=False)
