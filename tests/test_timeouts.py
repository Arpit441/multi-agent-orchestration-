"""Timeout enforcement tests."""

import asyncio

import pytest

from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State


@register_node("slow")
class SlowNode:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        await asyncio.sleep(float(self.config.get("sleep", 2.0)))
        state.set("done", True)
        return state


@pytest.mark.asyncio
async def test_timeout_retries_then_fails():
    policy = RetryPolicy(max_attempts=2, initial_delay_seconds=0, timeout_seconds=0.05)
    graph = (
        GraphBuilder("timeout_demo")
        .add_node("slow", "slow", config={"sleep": 1.0}, retry_policy=policy)
        .set_entry("slow")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    run = await runner.execute({})
    assert run.status == RunStatus.FAILED
    assert "timed out" in (run.error or "").lower()
    assert len(run.trace) == 2
