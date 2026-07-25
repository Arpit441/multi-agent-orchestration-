"""Retry and timeout policies for nodes."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BackoffStrategy(str, Enum):
    FIXED = "fixed"
    EXPONENTIAL = "exponential"


class RetryPolicy(BaseModel):
    """Per-node retry / timeout configuration."""

    max_attempts: int = Field(default=3, ge=1)
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_seconds: float = Field(default=0.5, ge=0.0)
    max_delay_seconds: float = Field(default=30.0, ge=0.0)
    timeout_seconds: float | None = Field(default=60.0, ge=0.0)
    # When retries are exhausted, route to this node instead of failing the run.
    fallback_node: str | None = None

    def delay_for_attempt(self, attempt: int) -> float:
        """Return sleep seconds before the given 1-based attempt (after a failure)."""
        if attempt <= 1:
            return 0.0
        if self.backoff == BackoffStrategy.FIXED:
            delay = self.initial_delay_seconds
        else:
            delay = self.initial_delay_seconds * (2 ** (attempt - 2))
        return min(delay, self.max_delay_seconds)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> RetryPolicy:
        if not config:
            return cls()
        return cls.model_validate(config)
