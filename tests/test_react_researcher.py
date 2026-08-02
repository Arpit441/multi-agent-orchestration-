"""Tests for LLM agent tool-calling / Researcher ReAct graph."""

from __future__ import annotations

import json

import pytest

from agent_orchestrator.core.graph import GraphBuilder
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.state import State
from agent_orchestrator.examples.research_report_pipeline import build_research_report_graph
from agent_orchestrator.nodes.agent_tools import Tool, make_researcher_tools
from agent_orchestrator.nodes.llm_agent_node import LLMAgentNode


class _FakeClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls = 0

    async def generate_json(self, **kwargs):  # noqa: ANN003
        if self.calls >= len(self.payloads):
            return {"done": True, "research_brief": "fallback"}, 10
        payload = self.payloads[self.calls]
        self.calls += 1
        return payload, 25

    async def generate(self, **kwargs):  # noqa: ANN003
        from agent_orchestrator.llm import GenerateResult

        text = json.dumps(self.payloads[min(self.calls, len(self.payloads) - 1)])
        self.calls += 1
        return GenerateResult(text=text, tokens_used=20)


@pytest.mark.asyncio
async def test_llm_agent_without_tools_single_call(monkeypatch):
    client = _FakeClient([{"approved": True, "score": 9, "feedback": "ok"}])
    monkeypatch.setattr(
        "agent_orchestrator.nodes.llm_agent_node.get_gemini_client",
        lambda: client,
    )
    node = LLMAgentNode(
        "critic",
        {
            "system_prompt": "score it",
            "user_template": "Report: {report}",
            "json_mode": True,
            "output_key": "critic_output",
        },
    )
    state = await node.run(State(data={"report": "hello"}))
    assert client.calls == 1
    assert state.get("score") == 9
    assert state.get("critic_output")["approved"] is True


@pytest.mark.asyncio
async def test_react_loop_executes_tools_and_stores_trace(monkeypatch):
    calls: list[str] = []

    async def search_handler(arguments, state):  # noqa: ANN001
        calls.append("search_web")
        state.set("search_results", [{"title": "A", "href": "https://example.com", "body": "note"}])
        return {"count": 1, "query": arguments.get("query")}

    async def synth_handler(arguments, state):  # noqa: ANN001
        calls.append("synthesize_findings")
        brief = "Edge AI brief"
        state.set("research_brief", brief)
        state.set("research_plan", {"research_brief": brief, "search_queries": ["edge AI"]})
        state.set("researcher_done", True)
        return {"research_brief": brief, "search_queries": ["edge AI"], "done": True}

    tools = [
        Tool("search_web", "search", {"query": "q"}, handler=search_handler),
        Tool("synthesize_findings", "done", {}, handler=synth_handler),
    ]
    client = _FakeClient(
        [
            {
                "thought": "Need sources",
                "tool_call": {"name": "search_web", "arguments": {"query": "edge AI"}},
            },
            {
                "thought": "Enough to wrap up",
                "tool_call": {"name": "synthesize_findings", "arguments": {}},
            },
        ]
    )
    monkeypatch.setattr(
        "agent_orchestrator.nodes.llm_agent_node.get_gemini_client",
        lambda: client,
    )

    node = LLMAgentNode(
        "researcher",
        {
            "system_prompt": "research",
            "user_template": "Topic: {topic}",
            "tools": tools,
            "max_tool_iterations": 3,
            "max_tool_calls": 3,
            "tool_trace_key": "research_trace",
            "output_key": "research_plan",
            "json_mode": True,
            "flatten_keys": ["research_brief", "search_queries"],
        },
    )
    state = await node.run(State(data={"topic": "edge AI"}))

    assert calls == ["search_web", "synthesize_findings"]
    assert state.get("research_brief") == "Edge AI brief"
    trace = state.get("research_trace")
    assert isinstance(trace, list) and len(trace) == 2
    assert trace[0]["action"] == "search_web"
    assert trace[0]["thought"] == "Need sources"
    assert trace[1]["action"] == "synthesize_findings"
    assert state.get("tool_calls_used") == 2


@pytest.mark.asyncio
async def test_budget_forces_synthesize_on_last_call(monkeypatch):
    calls: list[str] = []

    async def search_handler(arguments, state):  # noqa: ANN001
        calls.append("search_web")
        return {"count": 0}

    async def synth_handler(arguments, state):  # noqa: ANN001
        calls.append("synthesize_findings")
        state.set("researcher_done", True)
        state.set(
            "research_plan",
            {"research_brief": "forced", "search_queries": []},
        )
        state.set("research_brief", "forced")
        return {"research_brief": "forced", "done": True}

    tools = [
        Tool("search_web", "search", {"query": "q"}, handler=search_handler),
        Tool("synthesize_findings", "done", {}, handler=synth_handler),
    ]
    # First call searches; with max_tool_calls=2 the next turn must force synthesize
    # without a second LLM round requesting another search.
    client = _FakeClient(
        [
            {
                "thought": "search first",
                "tool_call": {"name": "search_web", "arguments": {"query": "x"}},
            },
            {
                "thought": "should not be used",
                "tool_call": {"name": "search_web", "arguments": {"query": "y"}},
            },
        ]
    )
    monkeypatch.setattr(
        "agent_orchestrator.nodes.llm_agent_node.get_gemini_client",
        lambda: client,
    )
    node = LLMAgentNode(
        "researcher",
        {
            "system_prompt": "research",
            "user_template": "Topic: {topic}",
            "tools": tools,
            "max_tool_iterations": 3,
            "max_tool_calls": 2,
            "tool_trace_key": "research_trace",
            "output_key": "research_plan",
        },
    )
    state = await node.run(State(data={"topic": "x"}))
    assert calls == ["search_web", "synthesize_findings"]
    assert state.get("research_brief") == "forced"
    assert state.get("tool_calls_used") == 2
    # Second LLM payload must not have been consumed for another search.
    assert client.calls == 1


def test_research_graph_researcher_owns_gathering():
    graph = build_research_report_graph()
    assert "web_search" not in graph.nodes
    assert "researcher" in graph.nodes
    assert any(e.source == "researcher" and e.target == "writer" for e in graph.edges)
    assert not any(e.source == "researcher" and e.target == "web_search" for e in graph.edges)

    node = graph.nodes["researcher"].instance
    tools = getattr(node, "tools", []) or []
    names = {t.name for t in tools}
    assert names == {"search_web", "browse_url", "synthesize_findings"}
    assert node.max_tool_calls == 3
    assert node.tool_trace_key == "research_trace"


def test_make_researcher_tools_schema():
    tools = make_researcher_tools()
    assert [t.name for t in tools] == [
        "search_web",
        "browse_url",
        "synthesize_findings",
    ]
