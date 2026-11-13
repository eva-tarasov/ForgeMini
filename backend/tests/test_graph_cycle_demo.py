import asyncio
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from model_test_support import offline_model_tests

from app.agent_runtime import graph_cycle_demo as demo


TOPIC = "Обзор reducers"
CONFIG = {"recursion_limit": 16}


def _message_parts(messages):
    return [(type(message), message.id, message.content) for message in messages]


def _expected_history(topic):
    return [
        (HumanMessage, "demo-user", topic),
        (AIMessage, "demo-draft-1", f"Training draft 1: {topic}"),
        (AIMessage, "demo-draft-2", f"Prepared training preview: {topic}"),
    ]


def test_draft_cycle_nodes_return_deltas_without_mutating_state():
    inputs = {"topic": TOPIC}
    assert demo.initialize(inputs) == {
        "messages": [HumanMessage(id="demo-user", content=TOPIC)],
        "rounds": 0,
    }
    assert inputs == {"topic": TOPIC}

    state = {
        **inputs,
        "messages": [
            HumanMessage(id="demo-user", content=TOPIC),
            AIMessage(id="demo-draft-1", content=f"Training draft 1: {TOPIC}"),
        ],
        "rounds": 1,
    }
    before = deepcopy(state)
    assert demo.draft_round(state) == {
        "messages": [AIMessage(id="demo-draft-2", content=f"Training draft 2: {TOPIC}")],
        "rounds": 1,
    }
    assert state == before

    state["rounds"] = 2
    state["messages"].append(AIMessage(id="demo-draft-2", content=f"Training draft 2: {TOPIC}"))
    before = deepcopy(state)
    assert demo.finalize(state) == {
        "messages": [AIMessage(id="demo-draft-2", content=f"Prepared training preview: {TOPIC}")],
        "outcome": "prepared",
    }
    assert state == before


def test_draft_cycle_graph_accumulates_and_replaces_messages():
    graph = demo.create_draft_cycle_graph()
    inputs = {"topic": TOPIC}

    result = graph.invoke(inputs, config=CONFIG)

    assert inputs == {"topic": TOPIC}
    assert result["topic"] == TOPIC
    assert result["rounds"] == 2
    assert result["outcome"] == "prepared"
    assert _message_parts(result["messages"]) == _expected_history(TOPIC)


def test_draft_cycle_graph_stream_updates():
    graph = demo.create_draft_cycle_graph()
    events = list(graph.stream({"topic": TOPIC}, config=CONFIG, stream_mode="updates", version="v2"))

    assert [(part["type"], part["ns"]) for part in events] == [("updates", ())] * 4
    initial, first, second, final = [part["data"] for part in events]
    assert list(initial) == ["initialize"]
    assert list(first) == list(second) == ["draft_round"]
    assert list(final) == ["finalize"]
    assert initial["initialize"]["rounds"] == 0
    assert first["draft_round"]["rounds"] == second["draft_round"]["rounds"] == 1
    assert _message_parts(first["draft_round"]["messages"]) == [
        (AIMessage, "demo-draft-1", f"Training draft 1: {TOPIC}")
    ]
    assert _message_parts(second["draft_round"]["messages"]) == [
        (AIMessage, "demo-draft-2", f"Training draft 2: {TOPIC}")
    ]
    assert _message_parts(final["finalize"]["messages"]) == _expected_history(TOPIC)[-1:]
    assert final["finalize"]["outcome"] == "prepared"
    assert "rounds" not in final["finalize"]


def test_draft_cycle_graph_stream_values_matches_invoke():
    graph = demo.create_draft_cycle_graph()
    inputs = {"topic": TOPIC}
    events = list(graph.stream(inputs, config=CONFIG, stream_mode="values", version="v2"))

    assert all(part["type"] == "values" and part["ns"] == () for part in events)
    final = events[-1]["data"]
    assert final["rounds"] == 2
    assert final == graph.invoke(inputs, config=CONFIG)


def test_draft_cycle_graph_astream_matches_ainvoke():
    graph = demo.create_draft_cycle_graph()
    inputs = {"topic": TOPIC}

    async def run():
        events = [
            part
            async for part in graph.astream(inputs, config=CONFIG, stream_mode="values", version="v2")
        ]
        return events, await graph.ainvoke(inputs, config=CONFIG)

    events, result = asyncio.run(run())

    assert events[-1]["type"] == "values"
    assert events[-1]["data"] == result
    assert result["rounds"] == 2
    assert result["outcome"] == "prepared"
    assert _message_parts(result["messages"]) == _expected_history(TOPIC)


def test_draft_cycle_graph_recursion_limit(monkeypatch):
    calls = []
    original = demo.finalize

    def observe_finalize(state: demo.DraftCycleState):
        calls.append(state["rounds"])
        return original(state)

    monkeypatch.setattr(demo, "finalize", observe_finalize)
    graph = demo.create_draft_cycle_graph()

    with pytest.raises(GraphRecursionError):
        graph.invoke({"topic": TOPIC}, config={"recursion_limit": 1})

    assert calls == []


def test_draft_cycle_graph_runs_are_independent():
    graph = demo.create_draft_cycle_graph()
    first = graph.invoke({"topic": "Первый обзор"}, config=CONFIG)
    before = deepcopy(first)
    second = graph.invoke({"topic": "Второй обзор"}, config=CONFIG)

    assert first == before
    assert first["rounds"] == second["rounds"] == 2
    assert _message_parts(first["messages"]) == _expected_history("Первый обзор")
    assert _message_parts(second["messages"]) == _expected_history("Второй обзор")
