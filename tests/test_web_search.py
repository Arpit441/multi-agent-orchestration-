"""Web search query planning and source relevance tests."""

from types import SimpleNamespace

import pytest

from agent_orchestrator.api.cache_util import search_cache
from agent_orchestrator.core.state import State
from agent_orchestrator.nodes.tool_node import (
    _result_relevance,
    _search_queries,
    source_guard_tool,
    web_search_tool,
)


def test_queries_are_anchored_to_topic():
    topic = "quantum computing in healthcare"
    queries = _search_queries(
        ["clinical quantum computing evidence", "electric vehicle market"],
        topic,
    )
    assert queries[0] == "clinical quantum computing evidence"
    assert queries[1].startswith(topic)


def test_relevance_rejects_topic_drift_and_social_links():
    topic = "quantum computing healthcare"
    relevant = {
        "title": "Quantum computing applications in healthcare",
        "body": "Clinical research and medical optimization",
        "href": "https://example.org/quantum-healthcare",
    }
    drifted = {
        "title": "Electric vehicle market report",
        "body": "Battery sales and charging",
        "href": "https://example.com/ev",
    }
    social = {**relevant, "href": "https://linkedin.com/pulse/quantum"}
    assert _result_relevance(relevant, topic, topic) > 0
    assert _result_relevance(drifted, topic, topic) == -1
    assert _result_relevance(social, topic, topic) == -1


@pytest.mark.asyncio
async def test_web_search_keeps_only_relevant_verified_urls(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def text(self, query, max_results):
            return [
                {
                    "title": "Quantum computing applications in healthcare",
                    "body": "Evidence for clinical and medical optimization",
                    "href": "https://example.org/quantum-healthcare",
                },
                {
                    "title": "Electric vehicle market",
                    "body": "Battery sales",
                    "href": "https://example.com/ev",
                },
            ]

    monkeypatch.setitem(__import__("sys").modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    search_cache.clear()
    result = await web_search_tool(
        State(
            data={
                "topic": "quantum computing healthcare",
                "search_queries": ["quantum computing clinical evidence"],
            }
        ),
        {"query_key": "search_queries", "max_results": 5},
    )
    assert len(result["search_results"]) == 1
    assert result["search_results"][0]["href"] == "https://example.org/quantum-healthcare"
    assert "example.org/quantum-healthcare" in result["sources_markdown"]
    assert "example.com/ev" not in result["sources_markdown"]
    assert result["search_warning"] == ""


@pytest.mark.asyncio
async def test_source_guard_removes_urls_not_returned_by_search():
    state = State(
        data={
            "report": (
                "Evidence from [Verified](https://example.org/verified) and "
                "[Invented](https://fake.example/report).\n\n"
                "## Sources\n"
                "1. [Verified](https://example.org/verified)\n"
                "2. [Invented](https://fake.example/report)"
            ),
            "search_results": [
                {"title": "Verified", "href": "https://example.org/verified"}
            ],
        }
    )
    result = await source_guard_tool(state, {})
    assert "https://example.org/verified" in result["report"]
    assert "https://fake.example/report" not in result["report"]
    assert result["source_validation"]["removed_url_count"] == 2


@pytest.mark.asyncio
async def test_source_guard_drops_sources_section_when_search_is_empty():
    state = State(
        data={
            "report": "Report without evidence.\n\n## Sources\n1. Made up source",
            "search_results": [],
        }
    )
    result = await source_guard_tool(state, {})
    assert "## Sources" not in result["report"]
    assert result["source_validation"]["allowed_url_count"] == 0
