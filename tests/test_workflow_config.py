"""Workflow config defaults, deep-merge, ceilings, and feature gating."""

from __future__ import annotations

import pytest

from agent_orchestrator.api.workflow_config import (
    CONFIG_CEILINGS,
    RESEARCH_DEFAULT,
    SUPPORT_DEFAULT,
    apply_ceilings,
    apply_config_to_state,
    deep_merge,
    feature_enabled,
    get_workflow,
    resolve_workflow_config,
)
from agent_orchestrator.core.runner import GraphRunner, InMemoryStore
from agent_orchestrator.core.state import State
from agent_orchestrator.examples.research_report_pipeline import (
    _needs_auto_revise,
    _needs_human_review,
    build_research_report_graph,
)
from agent_orchestrator.examples.support_resolution_pipeline import (
    _approved_for_deliver,
    _force_human_pause,
    _route_faq_revision,
)


def test_research_defaults():
    wf = get_workflow("research_report")
    assert wf is not None
    cfg = wf.default_config.to_dict()
    assert cfg["budget"]["max_tokens_total"] == 8000
    assert cfg["features"]["debate_loop"] is False
    assert cfg["features"]["fact_check_critic"] is True
    assert cfg["features"]["react_researcher"] is True


def test_support_defaults():
    wf = get_workflow("support_resolution")
    assert wf is not None
    cfg = wf.default_config.to_dict()
    assert cfg["budget"]["max_tokens_total"] == 4000
    assert cfg["features"]["debate_loop"] is False
    assert cfg["features"]["fact_check_critic"] is False
    assert cfg["features"]["force_human_review"] is False


def test_deep_merge_nested():
    base = RESEARCH_DEFAULT.to_dict()
    merged = deep_merge(base, {"budget": {"max_tokens_total": 12000}, "features": {"debate_loop": True}})
    assert merged["budget"]["max_tokens_total"] == 12000
    assert merged["budget"]["max_latency_ms"] == base["budget"]["max_latency_ms"]
    assert merged["features"]["debate_loop"] is True
    assert merged["features"]["react_researcher"] is True


def test_ceilings_clamp_max_tokens():
    out = apply_ceilings({"budget": {"max_tokens_total": 999_999}, "features": {}})
    assert out["budget"]["max_tokens_total"] == CONFIG_CEILINGS["max_tokens_total"]


def test_resolve_default_source():
    cfg, source = resolve_workflow_config("research_report")
    assert source == "default"
    assert cfg["budget"]["max_tokens_total"] == 8000
    assert cfg["features"]["debate_loop"] is False


def test_resolve_deep_research_preset():
    cfg, source = resolve_workflow_config("research_report", ui_preset="deep_research")
    assert source == "override"
    assert cfg["budget"]["max_tokens_total"] == 15_000
    assert cfg["features"]["debate_loop"] is True


def test_resolve_force_human_preset():
    cfg, source = resolve_workflow_config(
        "support_resolution", ui_preset="force_human_review"
    )
    assert source == "override"
    assert cfg["features"]["force_human_review"] is True
    assert cfg["budget"]["max_tokens_total"] == 4000


def test_override_clamped_above_ceiling():
    cfg, source = resolve_workflow_config(
        "research_report",
        {"budget": {"max_tokens_total": 100_000}},
    )
    assert source == "override"
    assert cfg["budget"]["max_tokens_total"] == 50_000


def test_apply_config_to_state_sets_budget():
    state = apply_config_to_state(
        {"topic": "x"},
        SUPPORT_DEFAULT.to_dict(),
        source="default",
    )
    assert state["budget"]["max_tokens_total"] == 4000
    assert state["config_source"] == "default"
    assert state["workflow_config"]["features"]["fact_check_critic"] is False


def test_research_debate_off_skips_auto_revise():
    state = State(
        data={
            "score": 4,
            "revision_count": 0,
            "workflow_config": {"features": {"debate_loop": False}},
        }
    )
    assert _needs_auto_revise(state) is False
    assert _needs_human_review(state) is True


def test_support_force_human_pause():
    state = State(
        data={
            "approved": True,
            "revision_count": 0,
            "workflow_config": {"features": {"force_human_review": True}},
        }
    )
    assert _force_human_pause(state) is True
    assert _approved_for_deliver(state) is False


def test_support_debate_off_skips_revision_route():
    state = State(
        data={
            "approved": False,
            "revision_count": 0,
            "intent": "faq",
            "workflow_config": {"features": {"debate_loop": False}},
        }
    )
    assert _route_faq_revision(state) is False


@pytest.mark.asyncio
async def test_runner_logs_config_source():
    graph = build_research_report_graph()
    # Tiny stub: we only care about the config trace event on start.
    store = InMemoryStore()
    runner = GraphRunner(graph, store=store)
    cfg, source = resolve_workflow_config("research_report", ui_preset="deep_research")
    state = apply_config_to_state({"topic": "test topic"}, cfg, source=source)
    run = await runner.create_run(state)
    # Mark as if execute started logging without running the full LLM graph:
    runner._log_config_source(run)
    assert any(t.node_name == "workflow_config" for t in run.trace)
    evt = next(t for t in run.trace if t.node_name == "workflow_config")
    assert evt.output_snapshot["config_source"] == "override"
    assert "overridden" in evt.output_snapshot["message"]


def test_feature_enabled_helper():
    state = {"workflow_config": {"features": {"react_researcher": False}}}
    assert feature_enabled(state, "react_researcher", True) is False
    assert feature_enabled({}, "react_researcher", True) is True
