"""Budget tracker and circuit-breaker tests."""

from __future__ import annotations

import time

import pytest

from agent_orchestrator.core.budget import BudgetTracker, default_budget_fields, ensure_budget_state
from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import Budget, RunStatus, State
from agent_orchestrator.llm import GenerateResult


@register_node("budget_leaf")
class BudgetLeaf:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        # Simulate LLM spend without calling Gemini.
        BudgetTracker(state).record_tokens(int(self.config.get("tokens", 100)))
        state.set("path", self.name)
        state.set(f"{self.name}_done", True)
        return state


@register_node("budget_llm")
class BudgetLlm:
    """Minimal stand-in that respects fast_fallback like LLMAgentNode."""

    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.tools = config.get("tools") or ["dummy"]

    async def run(self, state: State) -> State:
        mode = state.get("execution_mode")
        state.set("saw_mode", mode)
        if mode == "fast_fallback":
            state.set("tools_skipped", True)
            BudgetTracker(state).record_tokens(50)
        else:
            state.set("tools_skipped", False)
            BudgetTracker(state).record_tokens(200)
            BudgetTracker(state).record_step(1)
        state.set("path", self.name)
        return state


def test_budget_model_defaults():
    b = Budget()
    assert b.max_tokens_total == 8000
    assert b.max_latency_ms == 30_000
    assert b.max_agent_steps == 5
    assert b.tokens_used == 0
    assert b.steps_taken == 0


def test_budget_tracker_can_afford_and_record():
    state = State(data=default_budget_fields())
    tracker = BudgetTracker(state)
    assert tracker.can_afford(100) is True
    tracker.record_tokens(7_900)
    assert tracker.can_afford(200) is False
    tracker.record_step(5)
    assert tracker.budget["steps_taken"] == 5
    assert tracker.can_afford(0) is False


def test_budget_tracker_trips_on_latency():
    state = State(
        data={
            "budget": {
                "max_tokens_total": 8000,
                "max_latency_ms": 10,
                "max_agent_steps": 5,
                "tokens_used": 0,
                "steps_taken": 0,
                "started_at_ms": int(time.time() * 1000) - 50,
            },
            "execution_mode": "agentic",
            "circuit_breaker_triggered": False,
        }
    )
    tracker = BudgetTracker(state)
    assert tracker.should_trip_circuit() is True
    newly = tracker.activate_fast_fallback()
    assert newly is True
    assert state.get("execution_mode") == "fast_fallback"
    assert state.get("circuit_breaker_triggered") is True
    assert tracker.activate_fast_fallback() is False


@pytest.mark.asyncio
async def test_runner_circuit_breaker_logs_and_continues():
    graph = (
        GraphBuilder("budget_demo")
        .add_node("a", "budget_leaf", config={"tokens": 5000})
        .add_node("b", "budget_llm", config={"tools": ["x"]})
        .add_node("c", "budget_leaf", config={"tokens": 10})
        .set_entry("a")
        .add_edge("a", "b")
        .add_edge("b", "c")
        .mark_terminal("c")
        .compile()
    )
    initial = default_budget_fields()
    initial["budget"]["max_tokens_total"] = 1000  # trip after node a
    runner = GraphRunner(graph, InMemoryStore())
    run = await runner.execute(initial)
    assert run.status == RunStatus.COMPLETED
    assert run.state.get("circuit_breaker_triggered") is True
    assert run.state.get("execution_mode") == "fast_fallback"
    assert run.state.get("tools_skipped") is True
    assert any(t.outcome == "circuit_breaker" for t in run.trace)
    assert run.state.get("c_done") is True


@pytest.mark.asyncio
async def test_llm_agent_fast_fallback_skips_tools(monkeypatch):
    from agent_orchestrator.nodes.agent_tools import Tool
    from agent_orchestrator.nodes.llm_agent_node import LLMAgentNode

    calls: list[str] = []

    async def search_handler(arguments, state):  # noqa: ANN001
        calls.append("search_web")
        return {"count": 0}

    class Client:
        async def generate_json(self, **kwargs):  # noqa: ANN003
            return {"research_brief": "quick brief", "search_queries": ["q"], "done": True}, 40

        async def generate(self, **kwargs):  # noqa: ANN003
            return GenerateResult(text="hello", tokens_used=12)

    monkeypatch.setattr(
        "agent_orchestrator.nodes.llm_agent_node.get_gemini_client",
        lambda: Client(),
    )
    node = LLMAgentNode(
        "researcher",
        {
            "system_prompt": "research",
            "user_template": "Topic: {topic}",
            "tools": [
                Tool("search_web", "s", {"query": "q"}, handler=search_handler),
            ],
            "json_mode": True,
            "output_key": "research_plan",
            "flatten_keys": ["research_brief", "search_queries"],
        },
    )
    state = State(
        data={
            "topic": "x",
            **default_budget_fields(),
            "execution_mode": "fast_fallback",
            "circuit_breaker_triggered": True,
        }
    )
    out = await node.run(state)
    assert calls == []
    assert out.get("research_brief") == "quick brief"
    assert out.get("budget")["tokens_used"] >= 40


def test_ensure_budget_state_fills_defaults():
    state = State(data={})
    budget = ensure_budget_state(state)
    assert budget["max_tokens_total"] == 8000
    assert state.get("execution_mode") == "agentic"
    assert state.get("circuit_breaker_triggered") is False
