"""Fixer agent recovers a failed node and retries once."""

import pytest

from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State


@register_node("flaky_once")
class FlakyOnce:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1)
        self.calls = 0

    async def run(self, state: State) -> State:
        self.calls += 1
        # Fail until fixer has written recovery notes.
        if not state.get("recovery_notes"):
            raise RuntimeError("boom: missing recovery notes")
        state.set("report", f"recovered:{state.get('recovery_notes')}")
        return state


@register_node("fixer_stub")
class FixerStub:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1)

    async def run(self, state: State) -> State:
        state.set("recovery_notes", "use simpler output")
        state.set("fixer_output", {"recovery_notes": "use simpler output"})
        return state


@register_node("finish_ok")
class FinishOk:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("done", True)
        return state


@pytest.mark.asyncio
async def test_fixer_recovers_failed_agent():
    graph = (
        GraphBuilder("fixer_demo")
        .add_node(
            "writer",
            "flaky_once",
            retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=5),
        )
        .add_node(
            "fixer",
            "fixer_stub",
            retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=5),
        )
        .add_node("deliver", "finish_ok")
        .set_entry("writer")
        .add_edge("writer", "deliver")
        .mark_terminal("deliver")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    run = await runner.execute({"recovery_notes": "", "fixer_used_for": []})
    assert run.status == RunStatus.COMPLETED
    assert "recovered:" in (run.state.get("report") or "")
    assert run.state.get("recovery_applied") is True
    assert "writer" in (run.state.get("fixer_used_for") or [])
    # Trace should include fixer then a successful writer retry.
    nodes = [e.node_name for e in run.trace]
    assert "fixer" in nodes
    assert nodes.count("writer") >= 2
