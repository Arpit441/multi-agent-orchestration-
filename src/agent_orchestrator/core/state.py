"""Serializable graph state and run status machine."""

from __future__ import annotations

import copy
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from agent_orchestrator.core.errors import IllegalStateTransition


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


class State(BaseModel):
    """Explicit serializable state passed between nodes."""

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
