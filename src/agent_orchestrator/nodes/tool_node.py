"""Tool / function-call node."""

from __future__ import annotations

import re
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse

from agent_orchestrator.core.errors import NonRetryableError, RetryableError
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.state import State

ToolFn = Callable[[State, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]

_TOOL_REGISTRY: dict[str, ToolFn] = {}


def register_tool(name: str, fn: ToolFn) -> None:
    _TOOL_REGISTRY[name] = fn


def get_tool(name: str) -> ToolFn:
    if name not in _TOOL_REGISTRY:
        raise NonRetryableError(f"Unknown tool: {name}")
    return _TOOL_REGISTRY[name]


_SEARCH_STOPWORDS = {
    "about", "after", "before", "from", "into", "market", "report",
    "research", "study", "that", "their", "this", "using", "what", "with",
}
_LOW_QUALITY_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "pinterest.com",
    "quora.com", "reddit.com", "tiktok.com",
}


def _search_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 4 and token not in _SEARCH_STOPWORDS
    }


def _search_queries(raw: Any, topic: str, limit: int = 3) -> list[str]:
    """Normalize planner output and keep every query anchored to the topic."""
    if isinstance(raw, list):
        candidates = [str(item).strip() for item in raw]
    elif isinstance(raw, str) and raw.strip():
        candidates = [
            line.strip(" -•\t\"'`")
            for line in raw.splitlines()
            if line.strip()
        ]
    else:
        candidates = []

    topic_terms = _search_terms(topic)
    queries: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        # Planner queries that drift away from the user's topic are re-anchored.
        query = candidate
        if topic_terms and not (_search_terms(candidate) & topic_terms):
            query = f"{topic} {candidate}"
        if query not in queries:
            queries.append(query[:300])
        if len(queries) >= limit:
            break
    return queries or [topic]


def _result_relevance(item: dict[str, Any], topic: str, query: str) -> int:
    """Reject obvious topic drift and rank useful citation candidates."""
    href = str(item.get("href") or "")
    host = urlparse(href).netloc.lower().removeprefix("www.")
    if not href.startswith(("http://", "https://")):
        return -1
    if any(host == bad or host.endswith(f".{bad}") for bad in _LOW_QUALITY_HOSTS):
        return -1

    haystack = f"{item.get('title') or ''} {item.get('body') or ''}"
    haystack_terms = _search_terms(haystack)
    topic_overlap = len(_search_terms(topic) & haystack_terms)
    if _search_terms(topic) and topic_overlap == 0:
        return -1
    query_overlap = len(_search_terms(query) & haystack_terms)
    authority_bonus = 2 if host.endswith((".gov", ".edu", ".org")) else 0
    return topic_overlap * 3 + query_overlap + authority_bonus


async def web_search_tool(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """Search planned queries, filter topic drift, and cache verified URLs."""
    from agent_orchestrator.api.cache_util import search_cache

    topic = str(state.get("topic") or "").strip()
    query_key = config.get("query_key", "search_queries")
    raw_queries = state.get(query_key) or state.get("search_queries") or config.get("query")
    if not topic and not raw_queries:
        raise NonRetryableError("web_search requires a topic or planned search queries")
    queries = _search_queries(raw_queries, topic or str(raw_queries))
    max_results = int(config.get("max_results", 5))
    cache_key = f"{topic}|{'|'.join(queries)}|{max_results}|v2"

    if cache_key in search_cache:
        cached = dict(search_cache[cache_key])
        cached["search_cache_hit"] = True
        return cached

    try:
        from ddgs import DDGS

        candidates: list[tuple[int, dict[str, Any]]] = []
        with DDGS() as ddgs:
            for query in queries:
                for item in ddgs.text(query, max_results=max_results):
                    score = _result_relevance(item, topic, query)
                    if score >= 0:
                        candidates.append((score, item))
    except Exception as exc:  # noqa: BLE001
        raise RetryableError(f"web_search failed: {exc}") from exc

    snippets: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True):
        href = str(item.get("href") or "")
        if href in seen_urls:
            continue
        seen_urls.add(href)
        snippets.append(
            {
                "title": item.get("title"),
                "href": href,
                "body": item.get("body"),
            }
        )
        if len(snippets) >= max_results:
            break

    sources_markdown = "\n".join(
        f"{i}. [{s.get('title') or s.get('href')}]({s.get('href')})"
        for i, s in enumerate(snippets, start=1)
        if s.get("href")
    )
    payload = {
        "search_results": snippets,
        "search_query": queries[0],
        "search_queries_used": queries,
        "research_notes": "\n\n".join(
            f"[{i}] {s['title']}: {s['body']}"
            for i, s in enumerate(snippets, start=1)
            if s.get("body")
        ),
        "sources_markdown": sources_markdown,
        "search_warning": (
            "" if snippets else
            "No relevant, verifiable web sources were returned. The report must not invent citations."
        ),
        "search_cache_hit": False,
    }
    search_cache[cache_key] = {k: v for k, v in payload.items() if k != "search_cache_hit"}
    return payload


async def knowledge_lookup_tool(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """Retrieve relevant chunks from uploaded organisation documents."""
    from agent_orchestrator.knowledge import get_knowledge_store

    parts: list[str] = []
    for key in ("subject", "message", "topic", "customer_plan"):
        val = state.get(key)
        if val:
            parts.append(str(val))
    query = " ".join(parts).strip() or str(config.get("query") or "")
    top_k = int(config.get("top_k", 5))
    store = get_knowledge_store()
    hits = store.retrieve(query, top_k=top_k) if query else []
    context = store.format_context(query, top_k=top_k)
    return {
        "knowledge_context": context,
        "knowledge_hits": hits,
        "knowledge_query": query,
        "knowledge_doc_count": len(store.list_documents()),
    }


async def source_guard_tool(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """Deterministically remove report URLs that were not returned by web search."""
    from agent_orchestrator.api.workflow_config import feature_enabled

    report = str(state.get("report") or "")
    if not feature_enabled(state, "fact_check_critic", True):
        return {
            "report": report,
            "source_validation": {
                "skipped": True,
                "reason": "fact_check_critic disabled",
                "allowed_url_count": 0,
                "removed_url_count": 0,
                "removed_urls": [],
                "valid": True,
            },
        }

    allowed = {
        str(item.get("href"))
        for item in (state.get("search_results") or [])
        if isinstance(item, dict) and item.get("href")
    }
    markdown_links = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    invalid: list[str] = []

    def _guard_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if url in allowed:
            return match.group(0)
        invalid.append(url)
        return label

    cleaned = markdown_links.sub(_guard_link, report)
    if not allowed:
        # Source sections are only valid when the search tool returned verified URLs.
        cleaned = re.sub(
            r"\n##\s+Sources\b.*\Z",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        ).rstrip()

    return {
        "report": cleaned,
        "source_validation": {
            "allowed_url_count": len(allowed),
            "removed_url_count": len(invalid),
            "removed_urls": invalid,
            "valid": not invalid,
        },
    }


register_tool("web_search", web_search_tool)
register_tool("knowledge_lookup", knowledge_lookup_tool)
register_tool("source_guard", source_guard_tool)


@register_node("tool")
class ToolNode:
    """Invokes a registered Python tool/function and merges results into state."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.tool_name: str = config.get("tool", name)

    async def run(self, state: State) -> State:
        from agent_orchestrator.core.budget import BudgetTracker

        tool = get_tool(self.tool_name)
        result = tool(state, self.config)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        if not isinstance(result, dict):
            raise NonRetryableError(
                f"Tool '{self.tool_name}' must return a dict, got {type(result)!r}"
            )
        state.update(result)
        state.set("last_tool", self.tool_name)
        BudgetTracker(state).record_step(1)
        return state
