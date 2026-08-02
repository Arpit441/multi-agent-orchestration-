"""Research-and-report multi-agent pipeline (reference use case)."""

from __future__ import annotations

from agent_orchestrator.core.budget import default_budget_fields
from agent_orchestrator.core.graph import Graph, GraphBuilder
from agent_orchestrator.core.policies import BackoffStrategy, RetryPolicy
from agent_orchestrator.core.state import State

# Ensure built-in node types are registered.
import agent_orchestrator.nodes  # noqa: F401
from agent_orchestrator.nodes.agent_tools import make_researcher_tools
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


def _debate_loop_enabled(state: State) -> bool:
    """Debate loops are off by default once workflow_config is applied."""
    from agent_orchestrator.api.workflow_config import feature_enabled

    cfg = state.get("workflow_config")
    if not isinstance(cfg, dict):
        # Unit tests / legacy runs without config keep prior auto-revise behaviour.
        return True
    return feature_enabled(state, "debate_loop", False)


def _needs_auto_revise(state: State) -> bool:
    if not _debate_loop_enabled(state):
        return False
    return (not _critic_passes(state)) and _revision_count(state) < MAX_AUTO_REVISIONS


def _needs_human_review(state: State) -> bool:
    """HITL when still below threshold — immediately if debate is off, else after budget."""
    if _critic_passes(state):
        return False
    if not _debate_loop_enabled(state):
        return True
    return _revision_count(state) >= MAX_AUTO_REVISIONS


def build_research_report_graph() -> Graph:
    """
    planner -> knowledge_lookup -> researcher (bounded ReAct) -> writer -> source_guard -> critic
      |-- (score >= threshold) --> deliver
      |-- (below threshold, revisions left) --> writer
      |-- (below threshold, budget spent) --> human_approve --> deliver

    Planner runs first and may activate a dynamic ``plan_queue``. When no plan is
    present, the runner follows these static edges (backward compatible).
    """
    builder = GraphBuilder("research_report")

    builder.add_node(
        "planner",
        "llm_agent",
        config={
            "system_prompt": (
                "You are the execution Planner for a research multi-agent system. "
                "Given the topic and the remaining budget, produce a cost-aware plan. "
                "Return JSON with keys: plan (array of {step_id, type, reason}), "
                "estimated_tokens (int), estimated_time_ms (int), "
                "recommendation (\"fast_path\" or \"agentic_path\"), reasoning (string). "
                "Allowed step types: research, write, critic. "
                "If estimated_tokens would exceed 80% of budget.max_tokens_total, you MUST "
                "simplify (fewer searches, no debate loops) and set recommendation to "
                "fast_path. Prefer agentic_path only when the budget comfortably fits."
            ),
            "user_template": (
                "Topic: {topic}\n\n"
                "Report type: {report_type}\n"
                "Report type instructions:\n{report_type_instructions}\n\n"
                "Budget (authoritative):\n"
                "- max_tokens_total: {budget_max_tokens}\n"
                "- max_latency_ms: {budget_max_latency_ms}\n"
                "- max_agent_steps: {budget_max_agent_steps}\n"
                "- tokens_used so far: {budget_tokens_used}\n"
                "- 80% token soft-cap: {budget_token_soft_cap}\n\n"
                "Organisation knowledge preview (may be empty):\n{knowledge_context}\n\n"
                "Propose the plan now. Return JSON only."
            ),
            "output_key": "planner_output",
            "json_mode": True,
            "flatten_keys": [
                "plan",
                "estimated_tokens",
                "estimated_time_ms",
                "recommendation",
                "reasoning",
            ],
            "temperature": 0.2,
            "emit_thinking": False,
        },
        retry_policy=RetryPolicy(
            max_attempts=2,
            backoff=BackoffStrategy.EXPONENTIAL,
            timeout_seconds=45,
        ),
    )

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
                "You are a senior research analyst running a bounded ReAct loop. "
                "Gather evidence yourself with tools, then finish with synthesize_findings. "
                "Prefer 1–2 focused search_web calls; use browse_url only for the most "
                "promising source. Stay on the user's topic — do not drift. "
                "Respect the tool budget in every turn. When the budget is nearly gone, "
                "call synthesize_findings immediately."
            ),
            "user_template": (
                "Topic: {topic}\n\n"
                "Organisation knowledge (may be empty):\n{knowledge_context}\n\n"
                "RECOVERY NOTES FROM FIXER (follow if present):\n{recovery_notes}\n\n"
                "Investigate the topic with your tools, then synthesize_findings."
            ),
            "tools": make_researcher_tools(),
            "max_tool_iterations": 3,
            "max_tool_calls": 3,
            "tool_trace_key": "research_trace",
            "output_key": "research_plan",
            "json_mode": True,
            "flatten_keys": ["research_brief", "search_queries"],
            "temperature": 0.3,
        },
        retry_policy=RetryPolicy(
            max_attempts=3,
            backoff=BackoffStrategy.EXPONENTIAL,
            timeout_seconds=180,
        ),
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
                "Research ReAct trace (thought → action → observation):\n{research_trace_text}\n\n"
                "Search notes:\n{research_notes}\n\n"
                "Search status:\n{search_warning}\n\n"
                "Organisation knowledge (uploaded docs):\n{knowledge_context}\n\n"
                "Verified web sources (the only URLs you may cite):\n{sources_markdown}\n\n"
                "Other revision notes:\n{feedback}\n\n"
                "RECOVERY NOTES FROM FIXER (follow if present):\n{recovery_notes}\n\n"
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
                "RECOVERY NOTES FROM FIXER (follow if present):\n{recovery_notes}\n\n"
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
        "fixer",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a recovery specialist. Another agent failed. Diagnose the error "
                "and write concrete recovery notes so that agent can succeed on retry. "
                "Focus on formatting (valid JSON), missing fields, safer queries, or "
                "simpler output. Return JSON with keys: recovery_notes (string), "
                "likely_cause (string)."
            ),
            "user_template": (
                "Failed agent: {failed_node}\n"
                "Error:\n{failure_error}\n\n"
                "Topic: {topic}\n"
                "Current brief:\n{research_brief}\n"
                "Current feedback:\n{feedback}\n\n"
                "Return JSON only."
            ),
            "output_key": "fixer_output",
            "json_mode": True,
            "flatten_keys": ["recovery_notes", "likely_cause"],
            "temperature": 0.2,
        },
        retry_policy=RetryPolicy(max_attempts=2, timeout_seconds=60),
    )

    builder.add_node(
        "deliver",
        "tool",
        config={"tool": "deliver"},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=10),
    )

    builder.set_entry("planner")
    builder.add_edge("planner", "knowledge_lookup")
    builder.add_edge("knowledge_lookup", "researcher")
    builder.add_edge("researcher", "writer")
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
    budget_fields = default_budget_fields()
    budget = dict(budget_fields["budget"])
    soft_cap = int(int(budget.get("max_tokens_total") or 8000) * 0.8)
    return {
        "topic": topic,
        "report_type": report_type,
        "report_type_instructions": report_type_instructions(report_type),
        "feedback": "",
        "human_feedback": "",
        "previous_draft": "",
        "pending_human_revision": False,
        "recovery_notes": "",
        "fixer_used_for": [],
        "research_plan": {},
        "research_brief": "",
        "research_trace": [],
        "research_trace_text": "",
        "search_queries": [],
        "search_queries_used": [],
        "search_warning": "",
        "research_notes": "",
        "search_results": [],
        "sources_markdown": "",
        "browsed_pages": [],
        "researcher_done": False,
        "report": "",
        "approved": False,
        "revision_count": 0,
        "knowledge_context": "",
        # Planner / dynamic plan (checkpointed with state)
        "planner_output": {},
        "execution_plan": [],
        "plan_recommendation": "",
        "plan_reasoning": "",
        "estimated_tokens": 0,
        "estimated_time_ms": 0,
        "plan_queue": [],
        "plan_cursor": 0,
        "use_dynamic_plan": False,
        # Flattened budget fields for the planner prompt template
        "budget_max_tokens": budget.get("max_tokens_total"),
        "budget_max_latency_ms": budget.get("max_latency_ms"),
        "budget_max_agent_steps": budget.get("max_agent_steps"),
        "budget_tokens_used": budget.get("tokens_used"),
        "budget_token_soft_cap": soft_cap,
        **budget_fields,
    }
