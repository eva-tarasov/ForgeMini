import asyncio
from functools import wraps

import pytest
from model_test_support import offline_model_tests

from app.agent_runtime import graph_foundation as foundation


@pytest.fixture
def observed_graph(monkeypatch):
    executed = []

    def observe(node):
        @wraps(node)
        def observed(state):
            executed.append(node.__name__)
            return node(state)

        return observed

    for name in ("normalize_goal", "prepare_preview", "block_preview"):
        monkeypatch.setattr(foundation, name, observe(getattr(foundation, name)))

    return foundation.create_task_preview_graph(), executed


def test_normalize_goal_returns_only_a_partial_update():
    state = {"goal": "  Подготовить обзор 🧪\n", "acceptance_criteria": ["Сохранить Unicode"]}

    assert foundation.normalize_goal(state) == {"normalized_goal": "Подготовить обзор 🧪"}
    assert state == {
        "goal": "  Подготовить обзор 🧪\n",
        "acceptance_criteria": ["Сохранить Unicode"],
    }


@pytest.mark.parametrize("normalized_goal,route", [("Обзор", "ready"), ("", "blocked")])
def test_route_preview_uses_normalized_goal(normalized_goal, route):
    state = {"goal": "  ", "acceptance_criteria": [], "normalized_goal": normalized_goal}

    assert foundation.route_preview(state) == route


def test_task_preview_graph_ready_branch(observed_graph):
    graph, executed = observed_graph
    state = {"goal": "  Подготовить обзор  ", "acceptance_criteria": ["Сохранить критерии"]}

    result = graph.invoke(state)

    assert result == {
        "goal": "  Подготовить обзор  ",
        "acceptance_criteria": ["Сохранить критерии"],
        "normalized_goal": "Подготовить обзор",
        "outcome": "prepared",
        "preview": "Goal: Подготовить обзор",
    }
    assert executed == ["normalize_goal", "prepare_preview"]
    assert state == {"goal": "  Подготовить обзор  ", "acceptance_criteria": ["Сохранить критерии"]}


@pytest.mark.parametrize("goal", ["", " \t\n"], ids=["empty", "whitespace"])
def test_task_preview_graph_blocked_branch(observed_graph, goal):
    graph, executed = observed_graph
    state = {"goal": goal, "acceptance_criteria": ["Сохранить критерии"]}

    result = graph.invoke(state)

    assert result == {
        "goal": goal,
        "acceptance_criteria": ["Сохранить критерии"],
        "normalized_goal": "",
        "outcome": "blocked",
        "blocked_reason": "empty_goal",
    }
    assert executed == ["normalize_goal", "block_preview"]


def test_task_preview_graph_ainvoke():
    graph = foundation.create_task_preview_graph()
    state = {"goal": "  Асинхронный обзор  ", "acceptance_criteria": ["Ответить кратко"]}

    result = asyncio.run(graph.ainvoke(state))

    assert result == {
        "goal": "  Асинхронный обзор  ",
        "acceptance_criteria": ["Ответить кратко"],
        "normalized_goal": "Асинхронный обзор",
        "outcome": "prepared",
        "preview": "Goal: Асинхронный обзор",
    }


def test_task_preview_graph_runs_are_independent():
    graph = foundation.create_task_preview_graph()

    first = graph.invoke({"goal": "First", "acceptance_criteria": ["Criterion A"]})
    second = graph.invoke({"goal": " ", "acceptance_criteria": ["Criterion B"]})

    assert first["preview"] == "Goal: First"
    assert first["acceptance_criteria"] == ["Criterion A"]
    assert "blocked_reason" not in first
    assert second["blocked_reason"] == "empty_goal"
    assert second["acceptance_criteria"] == ["Criterion B"]
    assert "preview" not in second


def test_task_preview_graph_draws_mermaid_locally():
    mermaid = foundation.create_task_preview_graph().get_graph().draw_mermaid()

    assert "graph TD" in mermaid
    assert "__start__ --> normalize_goal" in mermaid
    assert "normalize_goal -. &nbsp;ready&nbsp; .-> prepare_preview" in mermaid
    assert "normalize_goal -. &nbsp;blocked&nbsp; .-> block_preview" in mermaid
    assert "prepare_preview --> __end__" in mermaid
    assert "block_preview --> __end__" in mermaid
