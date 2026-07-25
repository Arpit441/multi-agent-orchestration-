"""Graph validation and conditional edge tests."""

import pytest

from agent_orchestrator.core.errors import GraphValidationError
from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State


@register_node("set_flag")
class SetFlag:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("flag", self.config.get("flag", True))
        return state


@register_node("leaf")
class Leaf:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("path", self.name)
        return state


def test_duplicate_node_rejected():
    b = GraphBuilder("g").add_node("a", "leaf")
    with pytest.raises(GraphValidationError):
        b.add_node("a", "leaf")


def test_missing_entry_rejected():
    with pytest.raises(GraphValidationError):
        GraphBuilder("g").add_node("a", "leaf").compile()


@pytest.mark.asyncio
async def test_conditional_branch():
    graph = (
        GraphBuilder("cond")
        .add_node("start", "set_flag", config={"flag": True})
        .add_node("yes", "leaf")
        .add_node("no", "leaf")
        .set_entry("start")
        .add_edge("start", "yes", condition=lambda s: bool(s.get("flag")), label="yes")
        .add_edge("start", "no", condition=lambda s: not bool(s.get("flag")), label="no")
        .mark_terminal("yes", "no")
        .compile()
    )
    run = await GraphRunner(graph, InMemoryStore()).execute({})
    assert run.status == RunStatus.COMPLETED
    assert run.state.get("path") == "yes"
