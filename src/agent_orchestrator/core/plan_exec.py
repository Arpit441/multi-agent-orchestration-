"""Dynamic execution plans produced by the Planner node."""

from __future__ import annotations

from typing import Any

from agent_orchestrator.core.budget import ensure_budget_state
from agent_orchestrator.core.state import State

# Logical plan step types → concrete graph node names (must exist in the graph).
PLAN_TYPE_TO_NODE: dict[str, str] = {
    "research": "researcher",
    "write": "writer",
    "critic": "critic",
}

FAST_PATH_PLAN: list[dict[str, Any]] = [
    {
        "step_id": "1",
        "type": "research",
        "reason": "Single bounded research pass (budget-aware fast path)",
    },
    {
        "step_id": "2",
        "type": "write",
        "reason": "Draft report without extra debate loops",
    },
    {
        "step_id": "3",
        "type": "critic",
        "reason": "One quality check",
    },
]

AGENTIC_PATH_PLAN: list[dict[str, Any]] = [
    {
        "step_id": "1",
        "type": "research",
        "reason": "Full ReAct research with tools",
    },
    {
        "step_id": "2",
        "type": "write",
        "reason": "Draft grounded report",
    },
    {
        "step_id": "3",
        "type": "critic",
        "reason": "Quality gate (may revise via static edges)",
    },
]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_steps(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        step_type = str(item.get("type") or "").strip().lower()
        if step_type not in PLAN_TYPE_TO_NODE:
            continue
        out.append(
            {
                "step_id": str(item.get("step_id") or idx),
                "type": step_type,
                "reason": str(item.get("reason") or ""),
            }
        )
    return out


def expand_plan_to_nodes(
    plan_steps: list[dict[str, Any]],
    available_nodes: set[str],
    *,
    include_source_guard: bool = True,
) -> list[str]:
    """Turn logical plan steps into an ordered list of graph node names.

    Always prefixes ``knowledge_lookup`` when present, and inserts ``source_guard``
    after each ``writer`` step when that node exists and fact-check is enabled.
    """
    queue: list[str] = []
    if "knowledge_lookup" in available_nodes:
        queue.append("knowledge_lookup")

    for step in plan_steps:
        node = PLAN_TYPE_TO_NODE.get(str(step.get("type") or "").lower())
        if not node or node not in available_nodes:
            continue
        if queue and queue[-1] == node:
            continue
        queue.append(node)
        if (
            node == "writer"
            and include_source_guard
            and "source_guard" in available_nodes
        ):
            queue.append("source_guard")

    # Drop consecutive duplicates (e.g. repeated guards).
    deduped: list[str] = []
    for name in queue:
        if not deduped or deduped[-1] != name:
            deduped.append(name)
    return deduped


def seed_fallback_plan(state: State, *, reason: str = "") -> None:
    """Install a deterministic fast-path plan when the Planner LLM is unavailable."""
    ensure_budget_state(state)
    plan = list(FAST_PATH_PLAN)
    note = reason.strip() or "Planner unavailable"
    state.set("execution_plan", plan)
    state.set("plan_recommendation", "fast_path")
    state.set(
        "plan_reasoning",
        f"Fallback plan used ({note}). Continuing with a bounded research → write → critic path.",
    )
    state.set("estimated_tokens", 2500)
    state.set("estimated_time_ms", 45_000)
    state.set(
        "planner_output",
        {
            "plan": plan,
            "estimated_tokens": 2500,
            "estimated_time_ms": 45_000,
            "recommendation": "fast_path",
            "reasoning": state.get("plan_reasoning"),
            "fallback": True,
        },
    )
    state.set("planner_degraded", True)
    state.set("use_dynamic_plan", False)
    state.set("plan_queue", [])
    state.set("plan_cursor", 0)
    if not state.get("circuit_breaker_triggered"):
        state.set("execution_mode", "fast_fallback")


def apply_planner_result(state: State, *, available_nodes: set[str] | None = None) -> bool:
    """Normalize planner JSON into state and build ``plan_queue``.

    Returns True when a usable dynamic plan was activated.
    Enforces: if estimated_tokens > 80% of max_tokens_total → fast_path + simplified plan.
    """
    from agent_orchestrator.api.workflow_config import feature_enabled

    ensure_budget_state(state)
    budget = state.get("budget") or {}
    max_tokens = _as_int(budget.get("max_tokens_total"), 8000)
    threshold = int(max_tokens * 0.8)

    payload = state.get("planner_output")
    if not isinstance(payload, dict):
        payload = {}

    plan = _normalize_steps(payload.get("plan") or state.get("execution_plan"))
    estimated_tokens = _as_int(
        payload.get("estimated_tokens", state.get("estimated_tokens")), 0
    )
    estimated_time_ms = _as_int(
        payload.get("estimated_time_ms", state.get("estimated_time_ms")), 0
    )
    recommendation = str(
        payload.get("recommendation") or state.get("plan_recommendation") or ""
    ).strip().lower()
    reasoning = str(payload.get("reasoning") or state.get("plan_reasoning") or "")

    if recommendation not in {"fast_path", "agentic_path"}:
        recommendation = "agentic_path" if estimated_tokens <= threshold else "fast_path"

    forced_fast = estimated_tokens > threshold
    if forced_fast:
        recommendation = "fast_path"
        plan = list(FAST_PATH_PLAN)
        if reasoning:
            reasoning = f"{reasoning} | Budget guard: estimated_tokens {estimated_tokens} > 80% of {max_tokens}."
        else:
            reasoning = (
                f"Budget guard forced fast_path: estimated_tokens {estimated_tokens} "
                f"> 0.8 * max_tokens_total ({threshold})."
            )

    if not plan:
        plan = list(FAST_PATH_PLAN if recommendation == "fast_path" else AGENTIC_PATH_PLAN)

    # Persist planner artifacts (checkpointed with the rest of state).
    state.set("execution_plan", plan)
    state.set("plan_recommendation", recommendation)
    state.set("plan_reasoning", reasoning)
    state.set("estimated_tokens", estimated_tokens)
    state.set("estimated_time_ms", estimated_time_ms)
    state.set(
        "planner_output",
        {
            "plan": plan,
            "estimated_tokens": estimated_tokens,
            "estimated_time_ms": estimated_time_ms,
            "recommendation": recommendation,
            "reasoning": reasoning,
        },
    )

    if recommendation == "fast_path":
        state.set("execution_mode", "fast_fallback")
    else:
        # Don't undo an already-tripped circuit breaker.
        if not state.get("circuit_breaker_triggered"):
            state.set("execution_mode", "agentic")

    # Feature flag: keep planner artifacts but follow static edges.
    if not feature_enabled(state, "dynamic_planning", True):
        state.set("use_dynamic_plan", False)
        state.set("plan_queue", [])
        state.set("plan_cursor", 0)
        return False

    nodes = available_nodes or set()
    include_guard = feature_enabled(state, "fact_check_critic", True)
    queue = (
        expand_plan_to_nodes(plan, nodes, include_source_guard=include_guard)
        if nodes
        else []
    )
    # Only activate dynamic plan when we can resolve at least one step to a graph node.
    if not queue:
        state.set("use_dynamic_plan", False)
        state.set("plan_queue", [])
        state.set("plan_cursor", 0)
        return False

    state.set("plan_queue", queue)
    state.set("plan_cursor", 0)
    state.set("use_dynamic_plan", True)
    return True


def next_planned_node(state: State) -> str | None:
    """Advance the plan cursor and return the next graph node, or None if exhausted."""
    if not state.get("use_dynamic_plan"):
        return None
    queue = state.get("plan_queue") or []
    if not isinstance(queue, list) or not queue:
        state.set("use_dynamic_plan", False)
        return None
    cursor = _as_int(state.get("plan_cursor"), 0)
    if cursor >= len(queue):
        state.set("use_dynamic_plan", False)
        return None
    node = str(queue[cursor])
    state.set("plan_cursor", cursor + 1)
    state.set("current_plan_step", node)
    return node
