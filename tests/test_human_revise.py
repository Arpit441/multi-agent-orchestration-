"""Human revision loops back to writer with feedback."""

import pytest

from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import RunStatus, State


@register_node("draft_once")
class DraftOnce:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        feedback = state.get("human_feedback") or state.get("feedback") or ""
        body = f"draft-with:{feedback}" if feedback else "first-draft"
        state.set("report", body)
        return state


@register_node("pause_hitl")
class PauseHitl:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config or {}
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1, timeout_seconds=None)

    async def run(self, state: State) -> State:
        from agent_orchestrator.core.errors import CheckpointPaused

        if state.get("checkpoint_resolved"):
            state.set("checkpoint_resolved", False)
            return state
        state.set("awaiting_human", True)
        raise CheckpointPaused("need human")


@register_node("finish")
class Finish:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("done", True)
        return state


@pytest.mark.asyncio
async def test_human_revise_sends_feedback_to_writer():
    graph = (
        GraphBuilder("revise_demo")
        .add_node("writer", "draft_once")
        .add_node("human_approve", "pause_hitl", config={"revise_to": "writer"})
        .add_node("deliver", "finish")
        .set_entry("writer")
        .add_edge("writer", "human_approve")
        .add_edge("human_approve", "deliver")
        .mark_terminal("deliver")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    paused = await runner.execute({})
    assert paused.status == RunStatus.PAUSED
    assert paused.state.get("report") == "first-draft"

    again = await runner.approve(
        paused.run_id,
        decision="revise",
        comment="Use only topic-related sources",
    )
    assert again.status == RunStatus.PAUSED
    assert again.state.get("human_feedback") == "Use only topic-related sources"
    assert again.state.get("feedback") == "Use only topic-related sources"
    assert again.state.get("previous_draft") == "first-draft"
    assert again.state.get("report") == "draft-with:Use only topic-related sources"
    assert again.state.get("human_revision_count") == 1
    assert again.state.get("pending_human_revision") is True
    assert again.current_node == "human_approve"

    done = await runner.approve(paused.run_id, decision="approve")
    assert done.status == RunStatus.COMPLETED
    assert done.state.get("done") is True


@pytest.mark.asyncio
async def test_revise_requires_comment():
    graph = (
        GraphBuilder("revise_need_comment")
        .add_node("writer", "draft_once")
        .add_node("human_approve", "pause_hitl", config={"revise_to": "writer"})
        .add_node("deliver", "finish")
        .set_entry("writer")
        .add_edge("writer", "human_approve")
        .add_edge("human_approve", "deliver")
        .mark_terminal("deliver")
        .compile()
    )
    runner = GraphRunner(graph, store=InMemoryStore())
    paused = await runner.execute({})
    with pytest.raises(RuntimeError, match="Revision requires feedback"):
        await runner.approve(paused.run_id, decision="revise", comment="  ")
