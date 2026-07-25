"""Crash recovery / resume-from-checkpoint tests."""

import pytest

from agent_orchestrator.core.errors import CheckpointPaused
from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.runner import GraphRunner
from agent_orchestrator.core.state import RunStatus, State
from agent_orchestrator.persistence import SQLiteStore


@register_node("step_a")
class StepA:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("a", True)
        return state


@register_node("step_b")
class StepB:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("b", True)
        return state


@register_node("hitl")
class Hitl:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1)

    async def run(self, state: State) -> State:
        if state.get("checkpoint_resolved"):
            state.set("checkpoint_resolved", False)
            return state
        raise CheckpointPaused("need human")


@register_node("step_c")
class StepC:
    def __init__(self, name, config, retry_policy=None):
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()

    async def run(self, state: State) -> State:
        state.set("c", True)
        return state


@pytest.mark.asyncio
async def test_resume_after_crash(tmp_path):
    db = tmp_path / "test.db"
    graph = (
        GraphBuilder("crash_demo")
        .add_node("a", "step_a")
        .add_node("b", "step_b")
        .add_node("c", "step_c")
        .set_entry("a")
        .add_edge("a", "b")
        .add_edge("b", "c")
        .mark_terminal("c")
        .compile()
    )

    store1 = SQLiteStore(db)
    runner1 = GraphRunner(graph, store=store1)
    run = await runner1.create_run({"x": 1})
    # Execute only first node by manually driving one step via execute then "crash"
    # Simulate: run until after node a by using a graph pause — instead, execute full,
    # then create a "crashed" mid-run by saving RUNNING with current_node=b after a done.

    # Run fully first to ensure DB works, then simulate crash mid-flight:
    await runner1.execute(run_id=run.run_id)
    assert (await store1.load_run(run.run_id)).status == RunStatus.COMPLETED

    # New crash scenario: pause at HITL, "crash", new process resumes after approve.
    graph2 = (
        GraphBuilder("hitl_demo")
        .add_node("a", "step_a")
        .add_node("pause", "hitl")
        .add_node("c", "step_c")
        .set_entry("a")
        .add_edge("a", "pause")
        .add_edge("pause", "c")
        .mark_terminal("c")
        .compile()
    )
    db2 = tmp_path / "hitl.db"
    store2 = SQLiteStore(db2)
    runner2 = GraphRunner(graph2, store=store2)
    paused = await runner2.execute({"topic": "t"})
    assert paused.status == RunStatus.PAUSED
    assert paused.state.get("a") is True
    run_id = paused.run_id

    # Simulate process restart: new store + runner on same DB.
    store3 = SQLiteStore(db2)
    runner3 = GraphRunner(graph2, store=store3)
    loaded = await store3.load_run(run_id)
    assert loaded is not None
    assert loaded.status == RunStatus.PAUSED
    assert loaded.state.get("a") is True

    finished = await runner3.approve(run_id, decision="approve", comment="lgtm")
    assert finished.status == RunStatus.COMPLETED
    assert finished.state.get("c") is True
    assert finished.state.get("human_decision") == "approve"


@pytest.mark.asyncio
async def test_snapshots_persisted(tmp_path):
    db = tmp_path / "snap.db"
    graph = (
        GraphBuilder("snap_demo")
        .add_node("a", "step_a")
        .add_node("b", "step_b")
        .set_entry("a")
        .add_edge("a", "b")
        .mark_terminal("b")
        .compile()
    )
    store = SQLiteStore(db)
    runner = GraphRunner(graph, store=store)
    run = await runner.execute({})
    assert run.status == RunStatus.COMPLETED
    snap = await store.latest_snapshot(run.run_id)
    assert snap is not None
    assert snap["node_name"] in {"a", "b"}
