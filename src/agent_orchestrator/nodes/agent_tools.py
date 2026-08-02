"""Agent-callable tools for LLM ReAct loops (distinct from graph ToolNode)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from agent_orchestrator.core.errors import NonRetryableError, RetryableError
from agent_orchestrator.core.state import State
from agent_orchestrator.nodes.tool_node import (
    _LOW_QUALITY_HOSTS,
    _result_relevance,
    _search_terms,
)

ToolHandler = Callable[
    [dict[str, Any], State],
    Awaitable[dict[str, Any] | str] | dict[str, Any] | str,
]


@dataclass
class Tool:
    """A single tool the base LLM agent can invoke during a ReAct loop."""

    name: str
    description: str
    parameters: dict[str, str] = field(default_factory=dict)
    handler: ToolHandler | None = None

    def schema_for_prompt(self) -> str:
        params = ", ".join(
            f"{key}: {desc}" for key, desc in (self.parameters or {}).items()
        )
        param_bit = f" args={{ {params} }}" if params else ""
        return f"- {self.name}{param_bit}: {self.description}"


def tools_prompt_block(tools: list[Tool]) -> str:
    if not tools:
        return ""
    lines = "\n".join(t.schema_for_prompt() for t in tools)
    return (
        "\n\nYou can call tools. Each response MUST be JSON in one of these shapes:\n"
        '1) {"thought": "...", "tool_call": {"name": "<tool>", "arguments": {...}}}\n'
        '2) {"thought": "...", "done": true, ...final fields}\n'
        f"Available tools:\n{lines}\n"
    )


async def invoke_tool(tool: Tool, arguments: dict[str, Any], state: State) -> Any:
    if tool.handler is None:
        raise NonRetryableError(f"Tool '{tool.name}' has no handler")
    result = tool.handler(arguments or {}, state)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[misc]
    return result


def observation_text(result: Any, limit: int = 2500) -> str:
    text = result if isinstance(result, str) else str(result)
    text = text.strip()
    if len(text) > limit:
        return text[: limit - 20] + "\n…[truncated]"
    return text


def _merge_search_hits(state: State, snippets: list[dict[str, Any]], query: str) -> None:
    existing = list(state.get("search_results") or [])
    seen = {str(item.get("href")) for item in existing if isinstance(item, dict)}
    for item in snippets:
        href = str(item.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        existing.append(item)
    state.set("search_results", existing)

    used = list(state.get("search_queries_used") or [])
    if query and query not in used:
        used.append(query)
    state.set("search_queries_used", used)
    state.set("search_queries", used)
    state.set("search_query", used[0] if used else query)

    sources_markdown = "\n".join(
        f"{i}. [{s.get('title') or s.get('href')}]({s.get('href')})"
        for i, s in enumerate(existing, start=1)
        if s.get("href")
    )
    state.set("sources_markdown", sources_markdown)
    state.set(
        "research_notes",
        "\n\n".join(
            f"[{i}] {s.get('title')}: {s.get('body')}"
            for i, s in enumerate(existing, start=1)
            if s.get("body")
        ),
    )
    state.set(
        "search_warning",
        ""
        if existing
        else "No relevant, verifiable web sources were returned. The report must not invent citations.",
    )


async def search_web_handler(arguments: dict[str, Any], state: State) -> dict[str, Any]:
    """Bounded DuckDuckGo search for one query; merges verified hits into state."""
    from agent_orchestrator.api.cache_util import search_cache

    query = str(arguments.get("query") or "").strip()
    topic = str(state.get("topic") or "").strip()
    if not query:
        raise NonRetryableError("search_web requires arguments.query")

    topic_terms = _search_terms(topic)
    if topic_terms and not (_search_terms(query) & topic_terms):
        query = f"{topic} {query}".strip()

    max_results = 5
    cache_key = f"react|{topic}|{query}|{max_results}|v1"
    if cache_key in search_cache:
        cached = dict(search_cache[cache_key])
        _merge_search_hits(state, list(cached.get("snippets") or []), query)
        return {
            "query": query,
            "count": len(cached.get("snippets") or []),
            "snippets": cached.get("snippets") or [],
            "cache_hit": True,
        }

    try:
        from ddgs import DDGS

        candidates: list[tuple[int, dict[str, Any]]] = []
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=max_results):
                score = _result_relevance(item, topic, query)
                if score >= 0:
                    candidates.append((score, item))
    except Exception as exc:  # noqa: BLE001
        raise RetryableError(f"search_web failed: {exc}") from exc

    snippets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True):
        href = str(item.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        snippets.append(
            {
                "title": item.get("title"),
                "href": href,
                "body": item.get("body"),
            }
        )
        if len(snippets) >= max_results:
            break

    search_cache[cache_key] = {"snippets": snippets}
    _merge_search_hits(state, snippets, query)
    return {
        "query": query,
        "count": len(snippets),
        "snippets": snippets,
        "cache_hit": False,
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


async def browse_url_handler(arguments: dict[str, Any], state: State) -> dict[str, Any]:
    """Fetch a page and return a truncated plain-text excerpt."""
    import httpx

    url = str(arguments.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise NonRetryableError("browse_url requires an http(s) URL")

    host = urlparse(url).netloc.lower().removeprefix("www.")
    if any(host == bad or host.endswith(f".{bad}") for bad in _LOW_QUALITY_HOSTS):
        return {"url": url, "ok": False, "error": "Host blocked as low-quality for research."}

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "AgentOrchestratorResearchBot/1.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            raw = response.text
    except Exception as exc:  # noqa: BLE001
        raise RetryableError(f"browse_url failed for {url}: {exc}") from exc

    if "html" in content_type or raw.lstrip().lower().startswith("<!doctype html") or "<html" in raw[:500].lower():
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            excerpt = parser.text()
        except Exception:  # noqa: BLE001
            excerpt = re.sub(r"<[^>]+>", " ", raw)
            excerpt = re.sub(r"\s+", " ", excerpt).strip()
    else:
        excerpt = raw.strip()

    excerpt = excerpt[:3500]
    browsed = list(state.get("browsed_pages") or [])
    browsed.append({"url": url, "excerpt": excerpt[:1200]})
    state.set("browsed_pages", browsed[-8:])

    notes = str(state.get("research_notes") or "")
    addition = f"\n\n[browse] {url}\n{excerpt[:900]}"
    state.set("research_notes", (notes + addition).strip())

    return {"url": url, "ok": True, "excerpt": excerpt, "chars": len(excerpt)}


async def synthesize_findings_handler(
    arguments: dict[str, Any], state: State
) -> dict[str, Any]:
    """Package gathered evidence into the research brief the writer consumes."""
    brief = str(
        arguments.get("research_brief")
        or arguments.get("brief")
        or state.get("research_brief")
        or ""
    ).strip()
    key_findings = str(arguments.get("key_findings") or "").strip()
    queries = arguments.get("search_queries")
    if not isinstance(queries, list):
        queries = list(state.get("search_queries_used") or state.get("search_queries") or [])
    queries = [str(q).strip() for q in queries if str(q).strip()][:5]

    if not brief:
        topic = state.get("topic") or "the topic"
        notes = str(state.get("research_notes") or "")[:1200]
        brief = (
            f"Research brief for {topic}.\n"
            f"Sources gathered: {len(state.get('search_results') or [])}.\n"
            f"Key notes:\n{notes or 'Limited web evidence; writer should stay conservative.'}"
        )
    if key_findings:
        brief = f"{brief}\n\nKey findings:\n{key_findings}".strip()

    results = list(state.get("search_results") or [])
    sources_markdown = str(state.get("sources_markdown") or "")
    if not sources_markdown and results:
        sources_markdown = "\n".join(
            f"{i}. [{s.get('title') or s.get('href')}]({s.get('href')})"
            for i, s in enumerate(results, start=1)
            if s.get("href")
        )

    plan = {
        "research_brief": brief,
        "search_queries": queries,
        "source_count": len(results),
    }
    state.set("research_plan", plan)
    state.set("research_brief", brief)
    state.set("search_queries", queries)
    state.set("sources_markdown", sources_markdown)
    state.set(
        "search_warning",
        ""
        if results
        else "No relevant, verifiable web sources were returned. The report must not invent citations.",
    )
    state.set("researcher_done", True)

    return {
        "research_brief": brief,
        "search_queries": queries,
        "source_count": len(results),
        "done": True,
    }


def make_researcher_tools() -> list[Tool]:
    """Tools for the bounded Researcher ReAct agent (max 3 calls)."""
    return [
        Tool(
            name="search_web",
            description="Search the web for the query and return ranked snippet cards.",
            parameters={"query": "focused search string anchored to the topic"},
            handler=search_web_handler,
        ),
        Tool(
            name="browse_url",
            description="Open a promising URL and read a plain-text excerpt.",
            parameters={"url": "http(s) URL to fetch"},
            handler=browse_url_handler,
        ),
        Tool(
            name="synthesize_findings",
            description=(
                "Mandatory wrap-up tool. Summarize evidence into research_brief / "
                "search_queries for the writer. Call this when done or when the budget is low."
            ),
            parameters={
                "research_brief": "concise brief of questions, angles, and evidence",
                "key_findings": "optional bullet-style findings",
                "search_queries": "optional list of queries that were most useful",
            },
            handler=synthesize_findings_handler,
        ),
    ]
