"""Serializable graph state and run status machine."""

from __future__ import annotations

import copy
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_orchestrator.core.errors import IllegalStateTransition

ExecutionMode = Literal["agentic", "fast_fallback"]


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


# Legal transitions — reject anything else loudly.
LEGAL_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.RETRYING,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.RETRYING: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.FAILED: set(),
    RunStatus.COMPLETED: set(),
}


def assert_transition(current: RunStatus, new: RunStatus) -> None:
    if new == current:
        return
    allowed = LEGAL_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise IllegalStateTransition(
            f"Illegal run status transition: {current.value} -> {new.value}"
        )


class Budget(BaseModel):
    """Run-level spend limits tracked under ``state.data['budget']``."""

    max_tokens_total: int = 8000
    max_latency_ms: int = 30_000
    max_agent_steps: int = 5
    tokens_used: int = 0
    steps_taken: int = 0
    started_at_ms: float | None = None


class State(BaseModel):
    """Explicit serializable state passed between nodes.

    Budget / circuit-breaker fields (also see ``BudgetTracker``):

    - ``budget``: :class:`Budget` dict (``max_tokens_total``, ``max_latency_ms``,
      ``max_agent_steps``, ``tokens_used``, ``steps_taken``, ``started_at_ms``)
    - ``execution_mode``: ``\"agentic\"`` | ``\"fast_fallback\"``
    - ``circuit_breaker_triggered``: bool
    """

    data: dict[str, Any] = Field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self.data.update(values)

    def clone(self) -> State:
        return State(data=copy.deepcopy(self.data))

    def to_json(self) -> str:
        return json.dumps(self.data, default=str)

    @classmethod
    def from_json(cls, raw: str) -> State:
        return cls(data=json.loads(raw) if raw else {})

    def diff(self, other: State) -> dict[str, Any]:
        """Return keys that changed between this state and ``other`` (after)."""
        before = self.data
        after = other.data
        changed: dict[str, Any] = {}
        keys = set(before) | set(after)
        for key in keys:
            if before.get(key) != after.get(key):
                changed[key] = {"before": before.get(key), "after": after.get(key)}
        return changed

    def budget_model(self) -> Budget:
        raw = self.get("budget") or {}
        if not isinstance(raw, dict):
            raw = {}
        return Budget.model_validate({**Budget().model_dump(), **raw})
