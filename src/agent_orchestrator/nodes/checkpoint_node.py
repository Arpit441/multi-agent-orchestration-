"""Human-in-the-loop checkpoint node."""

from __future__ import annotations

from typing import Any

from agent_orchestrator.core.errors import CheckpointPaused
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.state import State


@register_node("checkpoint")
class CheckpointNode:
    """Persists state and pauses the run until an external approve/resume signal."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1, timeout_seconds=None)
        self.message: str = config.get(
            "message", "Human approval required before continuing."
        )
        self.preview_keys: list[str] = list(config.get("preview_keys", []))

    async def run(self, state: State) -> State:
        # If already resolved by the approve API, pass through once.
        if state.get("checkpoint_resolved"):
            state.set("checkpoint_resolved", False)
            state.set("checkpoint_message", None)
            return state

        preview = {k: state.get(k) for k in self.preview_keys} if self.preview_keys else {}
        state.set("checkpoint_message", self.message)
        state.set("checkpoint_preview", preview)
        state.set("awaiting_human", True)
        raise CheckpointPaused(self.message)
