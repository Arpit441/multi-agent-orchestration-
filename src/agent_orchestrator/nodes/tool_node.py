"""Tool / function-call node."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

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


async def web_search_tool(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """DuckDuckGo search — no API key required. Results cached by query."""
    from agent_orchestrator.api.cache_util import search_cache

    query_key = config.get("query_key", "topic")
    query = state.get(query_key) or state.get("search_query") or config.get("query")
    if not query:
        raise NonRetryableError("web_search requires a query in state or config")
    max_results = int(config.get("max_results", 5))
    cache_key = f"{query}|{max_results}"

    if cache_key in search_cache:
        cached = dict(search_cache[cache_key])
        cached["search_cache_hit"] = True
        return cached

    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(str(query), max_results=max_results))
    except Exception as exc:  # noqa: BLE001
        raise RetryableError(f"web_search failed: {exc}") from exc

    snippets = []
    for item in results:
        snippets.append(
            {
                "title": item.get("title"),
                "href": item.get("href"),
                "body": item.get("body"),
            }
        )
    sources_markdown = "\n".join(
        f"{i}. [{s.get('title') or s.get('href')}]({s.get('href')})"
        for i, s in enumerate(snippets, start=1)
        if s.get("href")
    )
    payload = {
        "search_results": snippets,
        "search_query": str(query),
        "research_notes": "\n\n".join(
            f"[{i}] {s['title']}: {s['body']}"
            for i, s in enumerate(snippets, start=1)
            if s.get("body")
        ),
        "sources_markdown": sources_markdown,
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


register_tool("web_search", web_search_tool)
register_tool("knowledge_lookup", knowledge_lookup_tool)


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
        return state
