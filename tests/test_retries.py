"""Retry policy and runner retry behavior."""

import pytest

from agent_orchestrator.core.errors import NonRetryableError, RetryableError
from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import BackoffStrategy, RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State


@register_node("flaky")
class FlakyNode:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.calls = 0

    async def run(self, state: State) -> State:
        self.calls += 1
        fail_times = int(self.config.get("fail_times", 2))
        if self.calls <= fail_times:
            raise RetryableError(f"transient failure #{self.calls}")
        state.set("ok", True)
        state.set("calls", self.calls)
        return state


@register_node("fatal")
class FatalNode:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        raise NonRetryableError("bad input")


@register_node("pass_through")
class PassNode:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set(self.name, True)
        return state


def test_backoff_delay():
    policy = RetryPolicy(
        backoff=BackoffStrategy.EXPONENTIAL,
        initial_delay_seconds=0.5,
        max_delay_seconds=10,
    )
    assert policy.delay_for_attempt(1) == 0.0
    assert policy.delay_for_attempt(2) == 0.5
    assert policy.delay_for_attempt(3) == 1.0
    assert policy.delay_for_attempt(4) == 2.0


@pytest.mark.asyncio
async def test_retries_until_success():
    policy = RetryPolicy(max_attempts=5, initial_delay_seconds=0, timeout_seconds=5)
    graph = (
        GraphBuilder("retry_demo")
        .add_node("a", "flaky", config={"fail_times": 2}, retry_policy=policy)
        .set_entry("a")
        .mark_terminal("a")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    run = await runner.execute({})
    assert run.status == RunStatus.COMPLETED
    assert run.state.get("ok") is True
    assert run.state.get("calls") == 3
    assert sum(1 for t in run.trace if t.outcome == "error") == 2


@pytest.mark.asyncio
async def test_non_retryable_fails_immediately():
    policy = RetryPolicy(max_attempts=5, initial_delay_seconds=0)
    graph = (
        GraphBuilder("fatal_demo")
        .add_node("a", "fatal", retry_policy=policy)
        .set_entry("a")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    run = await runner.execute({})
    assert run.status == RunStatus.FAILED
    assert len(run.trace) == 1


@pytest.mark.asyncio
async def test_fallback_edge_on_retry_exhaustion():
    policy = RetryPolicy(
        max_attempts=2,
        initial_delay_seconds=0,
        fallback_node="degraded",
    )
    graph = (
        GraphBuilder("fallback_demo")
        .add_node("a", "flaky", config={"fail_times": 10}, retry_policy=policy)
        .add_node("degraded", "pass_through")
        .set_entry("a")
        .add_edge("a", "degraded", is_fallback=True)
        .mark_terminal("degraded")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    run = await runner.execute({})
    assert run.status == RunStatus.COMPLETED
    assert run.state.get("degraded") is True
