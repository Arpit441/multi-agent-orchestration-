"""Planner output normalization and plan-driven runner tests."""

from __future__ import annotations

import pytest

from agent_orchestrator.core.budget import default_budget_fields
from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.plan_exec import (
    apply_planner_result,
    expand_plan_to_nodes,
    next_planned_node,
)
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State
from agent_orchestrator.examples.research_report_pipeline import build_research_report_graph


def test_expand_plan_injects_knowledge_and_source_guard():
    queue = expand_plan_to_nodes(
        [
            {"step_id": "1", "type": "research", "reason": "r"},
            {"step_id": "2", "type": "write", "reason": "w"},
            {"step_id": "3", "type": "critic", "reason": "c"},
        ],
        {"knowledge_lookup", "researcher", "writer", "source_guard", "critic", "deliver"},
    )
    assert queue == [
        "knowledge_lookup",
        "researcher",
        "writer",
        "source_guard",
        "critic",
    ]


def test_budget_guard_forces_fast_path():
    state = State(
        data={
            **default_budget_fields(),
            "planner_output": {
                "plan": [
                    {"step_id": "1", "type": "research", "reason": "deep"},
                    {"step_id": "2", "type": "write", "reason": "draft"},
                    {"step_id": "3", "type": "critic", "reason": "debate"},
                    {"step_id": "4", "type": "critic", "reason": "more debate"},
                ],
                "estimated_tokens": 7000,  # > 0.8 * 8000
                "estimated_time_ms": 25000,
                "recommendation": "agentic_path",
                "reasoning": "want full agentic",
            },
        }
    )
    ok = apply_planner_result(
        state,
        available_nodes={"knowledge_lookup", "researcher", "writer", "source_guard", "critic"},
    )
    assert ok is True
    assert state.get("plan_recommendation") == "fast_path"
    assert state.get("execution_mode") == "fast_fallback"
    assert state.get("use_dynamic_plan") is True
    assert len(state.get("execution_plan")) == 3
    assert state.get("plan_queue")[0] == "knowledge_lookup"


def test_next_planned_node_advances_cursor():
    state = State(
        data={
            "use_dynamic_plan": True,
            "plan_queue": ["knowledge_lookup", "researcher", "writer"],
            "plan_cursor": 0,
        }
    )
    assert next_planned_node(state) == "knowledge_lookup"
    assert next_planned_node(state) == "researcher"
    assert state.get("plan_cursor") == 2
    assert next_planned_node(state) == "writer"
    assert next_planned_node(state) is None
    assert state.get("use_dynamic_plan") is False


def test_research_graph_starts_at_planner():
    graph = build_research_report_graph()
    assert graph.entry_point == "planner"
    assert "planner" in graph.nodes
    assert any(e.source == "planner" and e.target == "knowledge_lookup" for e in graph.edges)


@register_node("plan_stub")
class PlanStub:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("last", self.name)
        visited = list(state.get("visited") or [])
        visited.append(self.name)
        state.set("visited", visited)
        if self.name == "planner":
            state.set(
                "planner_output",
                {
                    "plan": [
                        {"step_id": "1", "type": "research", "reason": "r"},
                        {"step_id": "2", "type": "write", "reason": "w"},
                    ],
                    "estimated_tokens": 1000,
                    "estimated_time_ms": 5000,
                    "recommendation": "agentic_path",
                    "reasoning": "fits budget",
                },
            )
        if self.name == "writer":
            state.set("report", "done")
        return state


@pytest.mark.asyncio
async def test_runner_follows_dynamic_plan_then_edges():
    # Map research→researcher stub named researcher, write→writer.
    graph = (
        GraphBuilder("plan_demo")
        .add_node("planner", "plan_stub")
        .add_node("knowledge_lookup", "plan_stub")
        .add_node("researcher", "plan_stub")
        .add_node("writer", "plan_stub")
        .add_node("source_guard", "plan_stub")
        .add_node("critic", "plan_stub")
        .add_node("deliver", "plan_stub")
        .set_entry("planner")
        # Static fallback edges (used when no plan / after plan exhausts).
        .add_edge("planner", "knowledge_lookup")
        .add_edge("knowledge_lookup", "researcher")
        .add_edge("researcher", "writer")
        .add_edge("writer", "source_guard")
        .add_edge("source_guard", "critic")
        .add_edge("critic", "deliver")
        .mark_terminal("deliver")
        .compile()
    )
    runner = GraphRunner(graph, InMemoryStore())
    run = await runner.execute({**default_budget_fields(), "visited": []})
    assert run.status == RunStatus.COMPLETED
    assert run.state.get("plan_recommendation") == "agentic_path"
    assert run.state.get("use_dynamic_plan") is False
    # Plan queue path: planner, then knowledge → research → write → source_guard,
    # then plan ends; static edge critic → deliver.
    visited = run.state.get("visited")
    assert visited[0] == "planner"
    assert "knowledge_lookup" in visited
    assert "researcher" in visited
    assert "writer" in visited
    assert "source_guard" in visited
    assert visited[-1] == "deliver"


@pytest.mark.asyncio
async def test_runner_falls_back_to_edges_without_plan():
    graph = (
        GraphBuilder("no_plan")
        .add_node("planner", "plan_stub")
        .add_node("knowledge_lookup", "plan_stub")
        .add_node("deliver", "plan_stub")
        .set_entry("planner")
        .add_edge("planner", "knowledge_lookup")
        .add_edge("knowledge_lookup", "deliver")
        .mark_terminal("deliver")
        .compile()
    )

    # Override planner stub via a one-off node that clears plan.
    @register_node("empty_planner")
    class EmptyPlanner:
        def __init__(self, name, config, retry_policy=None):
            self.name = name
            self.config = config
            self.retry_policy = retry_policy or RetryPolicy()

        async def run(self, state: State) -> State:
            state.set("planner_output", {})
            state.set("visited", ["planner"])
            return state

    graph = (
        GraphBuilder("no_plan2")
        .add_node("planner", "empty_planner")
        .add_node("knowledge_lookup", "plan_stub")
        .add_node("deliver", "plan_stub")
        .set_entry("planner")
        .add_edge("planner", "knowledge_lookup")
        .add_edge("knowledge_lookup", "deliver")
        .mark_terminal("deliver")
        .compile()
    )
    run = await GraphRunner(graph, InMemoryStore()).execute(default_budget_fields())
    assert run.status == RunStatus.COMPLETED
    # apply_planner_result still builds a default plan when types resolve —
    # for this tiny graph research/write/critic nodes are missing, so dynamic
    # plan activation fails and static edges are used.
    assert run.state.get("visited") == ["planner", "knowledge_lookup", "deliver"]
