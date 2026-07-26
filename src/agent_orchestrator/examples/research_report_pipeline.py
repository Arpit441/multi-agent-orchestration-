"""Research-and-report multi-agent pipeline (reference use case)."""

from __future__ import annotations

from agent_orchestrator.core.graph import Graph, GraphBuilder
from agent_orchestrator.core.policies import BackoffStrategy, RetryPolicy
from agent_orchestrator.core.state import State

# Ensure built-in node types are registered.
import agent_orchestrator.nodes  # noqa: F401
from agent_orchestrator.nodes.tool_node import register_tool


async def deliver_tool(state: State, config: dict) -> dict:
    report = state.get("report") or state.get("writer_output") or ""
    return {
        "final_report": report,
        "delivered": True,
        "delivery_note": config.get(
            "note", "Report delivered after human sign-off."
        ),
    }


register_tool("deliver", deliver_tool)


# Presets that make the tool feel purpose-built for real scenarios.
REPORT_TYPES: dict[str, dict[str, str]] = {
    "general": {
        "label": "General research brief",
        "instructions": (
            "Produce a clear, well-structured research report with an executive "
            "summary, key findings, and a short conclusion."
        ),
    },
    "market_analysis": {
        "label": "Market analysis",
        "instructions": (
            "Produce a market analysis: market size & growth, key segments, major "
            "players, trends, risks, and an opportunities section. Use tables where useful."
        ),
    },
    "tech_comparison": {
        "label": "Technology comparison",
        "instructions": (
            "Produce a technology comparison: a comparison table of the main options, "
            "pros/cons each, ideal use cases, and a clear recommendation with justification."
        ),
    },
    "competitor_research": {
        "label": "Competitor research",
        "instructions": (
            "Produce competitor research: profile the top competitors, their positioning, "
            "strengths/weaknesses, pricing signals, and a differentiation opportunities section."
        ),
    },
    "literature_review": {
        "label": "Literature review",
        "instructions": (
            "Produce a literature-review-style summary: themes, what sources agree/disagree on, "
            "gaps, and directions for further investigation."
        ),
    },
}


def report_type_instructions(report_type: str) -> str:
    return REPORT_TYPES.get(report_type, REPORT_TYPES["general"])["instructions"]


CRITIC_SCORE_THRESHOLD = 7
MAX_AUTO_REVISIONS = 3


def _revision_count(state: State) -> int:
    try:
        return int(state.get("revision_count") or 0)
    except (TypeError, ValueError):
        return 0


def _critic_score(state: State) -> float | None:
    raw = state.get("score")
    critic = state.get("critic_output")
    if raw is None and isinstance(critic, dict):
        raw = critic.get("score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _critic_passes(state: State) -> bool:
    """Quality gate: score >= threshold (fallback to approved boolean)."""
    score = _critic_score(state)
    if score is not None:
        return score >= CRITIC_SCORE_THRESHOLD
    approved = state.get("approved")
    if isinstance(approved, bool):
        return approved
    if isinstance(approved, str):
        return approved.lower() in {"true", "yes", "approved"}
    critic = state.get("critic_output")
    if isinstance(critic, dict) and "approved" in critic:
        return bool(critic["approved"])
    return False


def _needs_auto_revise(state: State) -> bool:
    return (not _critic_passes(state)) and _revision_count(state) < MAX_AUTO_REVISIONS


def _needs_human_review(state: State) -> bool:
    """HITL only when still below threshold after auto-revision budget."""
    return (not _critic_passes(state)) and _revision_count(state) >= MAX_AUTO_REVISIONS


def build_research_report_graph() -> Graph:
    """
    researcher -> web_search -> writer -> source_guard -> critic
      |-- (score >= threshold) --> deliver
      |-- (below threshold, revisions left) --> writer
      |-- (below threshold, budget spent) --> human_approve --> deliver
    """
    builder = GraphBuilder("research_report")

    builder.add_node(
        "knowledge_lookup",
        "tool",
        config={"tool": "knowledge_lookup", "top_k": 5},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=15),
    )

    builder.add_node(
        "researcher",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a senior research analyst. Given a topic, produce a concise "
                "research brief: key questions, angles to investigate, and what evidence "
                "to look for. Use organisation knowledge context if provided. Be specific. "
                "Return JSON with exactly two keys: research_brief (string) and "
                "search_queries (array of 2-3 focused query strings). Every search query "
                "must preserve the user's core topic; do not drift to adjacent industries."
            ),
            "user_template": (
                "Topic: {topic}\n\n"
                "Organisation knowledge (may be empty):\n{knowledge_context}\n\n"
                "Create the research brief and focused web search queries. Return JSON only."
            ),
            "output_key": "research_plan",
            "json_mode": True,
            "flatten_keys": ["research_brief", "search_queries"],
            "temperature": 0.3,
        },
        retry_policy=RetryPolicy(
            max_attempts=3,
            backoff=BackoffStrategy.EXPONENTIAL,
            timeout_seconds=90,
        ),
    )

    builder.add_node(
        "web_search",
        "tool",
        config={
            "tool": "web_search",
            "query_key": "search_queries",
            "max_results": 6,
        },
        retry_policy=RetryPolicy(max_attempts=3, timeout_seconds=45),
    )

    builder.add_node(
        "writer",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a professional research writer. Draft a clear, well-structured "
                "markdown report grounded in the provided search notes — do not invent "
                "facts. If HUMAN REVISION REQUEST is present, you MUST revise the previous "
                "draft to satisfy every point in that request before anything else. "
                "Only cite URLs supplied in 'Verified web sources'; never invent, alter, or "
                "substitute a URL. If verified sources are present, cite them inline like "
                "[1], [2] and end with a matching '## Sources' section. If none are present, "
                "do not add citations or a Sources section; explicitly state that no verified "
                "web sources were available."
            ),
            "user_template": (
                "Topic: {topic}\n\n"
                "Report type instructions:\n{report_type_instructions}\n\n"
                "HUMAN REVISION REQUEST (highest priority — address every point):\n"
                "{human_feedback}\n\n"
                "Previous draft to revise (may be empty on first write):\n{previous_draft}\n\n"
                "Research brief:\n{research_brief}\n\n"
                "Search notes:\n{research_notes}\n\n"
                "Search status:\n{search_warning}\n\n"
                "Organisation knowledge (uploaded docs):\n{knowledge_context}\n\n"
                "Verified web sources (the only URLs you may cite):\n{sources_markdown}\n\n"
                "Other revision notes:\n{feedback}\n\n"
                "Write the full updated report in markdown. If a human revision request is "
                "present, start from the previous draft and apply those changes explicitly."
            ),
            "output_key": "report",
            "temperature": 0.4,
        },
        retry_policy=RetryPolicy(max_attempts=3, timeout_seconds=120),
    )

    builder.add_node(
        "source_guard",
        "tool",
        config={"tool": "source_guard"},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=10),
    )

    builder.add_node(
        "critic",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a strict editorial critic. Evaluate the report against: "
                "clarity, evidence, structure, and actionability. "
                "Return JSON with keys: approved (boolean), score (0-10), feedback (string). "
                f"Set approved=true only if score >= {CRITIC_SCORE_THRESHOLD}."
            ),
            "user_template": (
                "Topic: {topic}\n\nReport:\n{report}\n\n"
                "Organisation knowledge:\n{knowledge_context}\n\n"
                "Return JSON only."
            ),
            "output_key": "critic_output",
            "json_mode": True,
            "track_revisions": True,
            "temperature": 0.1,
        },
        retry_policy=RetryPolicy(max_attempts=3, timeout_seconds=90),
    )

    builder.add_node(
        "human_approve",
        "checkpoint",
        config={
            "message": (
                "Critic score stayed below the quality threshold after automatic revisions. "
                "Review the draft, then approve, reject, or request another revision."
            ),
            "preview_keys": ["topic", "report", "critic_output", "score"],
            "revise_to": "writer",
        },
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=None),
    )

    builder.add_node(
        "deliver",
        "tool",
        config={"tool": "deliver"},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=10),
    )

    builder.set_entry("knowledge_lookup")
    builder.add_edge("knowledge_lookup", "researcher")
    builder.add_edge("researcher", "web_search")
    builder.add_edge("web_search", "writer")
    builder.add_edge("writer", "source_guard")
    builder.add_edge("source_guard", "critic")
    builder.add_edge(
        "critic",
        "deliver",
        condition=_critic_passes,
        label="auto_deliver",
    )
    builder.add_edge(
        "critic",
        "writer",
        condition=_needs_auto_revise,
        label="auto_revise",
    )
    builder.add_edge(
        "critic",
        "human_approve",
        condition=_needs_human_review,
        label="needs_human",
    )
    builder.add_edge("human_approve", "deliver")
    builder.mark_terminal("deliver")

    return builder.compile()


def default_initial_state(topic: str, report_type: str = "general") -> dict:
    return {
        "topic": topic,
        "report_type": report_type,
        "report_type_instructions": report_type_instructions(report_type),
        "feedback": "",
        "human_feedback": "",
        "previous_draft": "",
        "pending_human_revision": False,
        "research_plan": {},
        "research_brief": "",
        "search_queries": [],
        "search_queries_used": [],
        "search_warning": "",
        "research_notes": "",
        "sources_markdown": "",
        "report": "",
        "approved": False,
        "revision_count": 0,
        "knowledge_context": "",
    }
