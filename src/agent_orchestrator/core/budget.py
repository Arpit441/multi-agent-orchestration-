"""Token / latency / step budget tracking and circuit-breaker helpers."""

from __future__ import annotations

import time
from typing import Any

from agent_orchestrator.core.state import State

DEFAULT_MAX_TOKENS_TOTAL = 8000
DEFAULT_MAX_LATENCY_MS = 120_000
DEFAULT_MAX_AGENT_STEPS = 5

DEFAULT_BUDGET: dict[str, Any] = {
    "max_tokens_total": DEFAULT_MAX_TOKENS_TOTAL,
    "max_latency_ms": DEFAULT_MAX_LATENCY_MS,
    "max_agent_steps": DEFAULT_MAX_AGENT_STEPS,
    "tokens_used": 0,
    "steps_taken": 0,
    "started_at_ms": None,
}


def default_budget_fields() -> dict[str, Any]:
    """Top-level state keys for a new run."""
    return {
        "budget": {
            **DEFAULT_BUDGET,
            "started_at_ms": int(time.time() * 1000),
        },
        "execution_mode": "agentic",
        "circuit_breaker_triggered": False,
    }


def ensure_budget_state(state: State) -> dict[str, Any]:
    """Merge defaults into ``state['budget']`` and related flags; return budget dict."""
    raw = state.get("budget")
    budget: dict[str, Any] = dict(DEFAULT_BUDGET)
    if isinstance(raw, dict):
        budget.update(raw)
    if budget.get("started_at_ms") is None:
        budget["started_at_ms"] = int(time.time() * 1000)
    # Normalize numeric fields.
    for key, default in (
        ("max_tokens_total", DEFAULT_MAX_TOKENS_TOTAL),
        ("max_latency_ms", DEFAULT_MAX_LATENCY_MS),
        ("max_agent_steps", DEFAULT_MAX_AGENT_STEPS),
        ("tokens_used", 0),
        ("steps_taken", 0),
    ):
        try:
            budget[key] = int(budget.get(key, default) or default)
        except (TypeError, ValueError):
            budget[key] = default
    state.set("budget", budget)
    if state.get("execution_mode") not in {"agentic", "fast_fallback"}:
        state.set("execution_mode", "agentic")
    if state.get("circuit_breaker_triggered") is None:
        state.set("circuit_breaker_triggered", False)
    return budget


class BudgetTracker:
    """Read/write budget counters on a State bag. Nodes call ``can_afford`` first."""

    def __init__(self, state: State) -> None:
        self.state = state
        ensure_budget_state(state)

    @property
    def budget(self) -> dict[str, Any]:
        return ensure_budget_state(self.state)

    def elapsed_ms(self) -> float:
        started = self.budget.get("started_at_ms")
        if started is None:
            return 0.0
        try:
            return max(0.0, (time.time() * 1000) - float(started))
        except (TypeError, ValueError):
            return 0.0

    def can_afford(self, cost: int) -> bool:
        """Return False if this token cost (or the run) would exceed budget limits."""
        if self.state.get("execution_mode") == "fast_fallback":
            return False
        if self.state.get("circuit_breaker_triggered"):
            return False
        b = self.budget
        cost = max(0, int(cost or 0))
        if b["tokens_used"] + cost > b["max_tokens_total"]:
            return False
        if b["steps_taken"] >= b["max_agent_steps"]:
            return False
        if self.elapsed_ms() >= b["max_latency_ms"]:
            return False
        return True

    def record_tokens(self, tokens: int) -> int:
        b = self.budget
        add = max(0, int(tokens or 0))
        b["tokens_used"] = int(b.get("tokens_used") or 0) + add
        self.state.set("budget", b)
        return b["tokens_used"]

    def record_step(self, steps: int = 1) -> int:
        b = self.budget
        add = max(0, int(steps or 0))
        b["steps_taken"] = int(b.get("steps_taken") or 0) + add
        self.state.set("budget", b)
        return b["steps_taken"]

    def should_trip_circuit(self) -> bool:
        b = self.budget
        return (
            b["tokens_used"] >= b["max_tokens_total"]
            or self.elapsed_ms() >= b["max_latency_ms"]
        )

    def trip_reason(self) -> str:
        b = self.budget
        reasons: list[str] = []
        if b["tokens_used"] >= b["max_tokens_total"]:
            reasons.append(
                f"tokens_used {b['tokens_used']} >= max_tokens_total {b['max_tokens_total']}"
            )
        if self.elapsed_ms() >= b["max_latency_ms"]:
            reasons.append(
                f"elapsed_ms {int(self.elapsed_ms())} >= max_latency_ms {b['max_latency_ms']}"
            )
        if b["steps_taken"] >= b["max_agent_steps"]:
            reasons.append(
                f"steps_taken {b['steps_taken']} >= max_agent_steps {b['max_agent_steps']}"
            )
        return "; ".join(reasons) or "budget exceeded"

    def activate_fast_fallback(self, reason: str | None = None) -> bool:
        """Flip to fast_fallback. Returns True if this call newly triggered it."""
        already = bool(self.state.get("circuit_breaker_triggered"))
        self.state.set("execution_mode", "fast_fallback")
        self.state.set("circuit_breaker_triggered", True)
        msg = reason or self.trip_reason()
        self.state.set("circuit_breaker_reason", msg)
        return not already
