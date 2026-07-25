"""Framework-level exceptions with retry classification."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base error for the orchestration framework."""


class IllegalStateTransition(OrchestratorError):
    """Raised when a run status transition is not allowed."""


class GraphValidationError(OrchestratorError):
    """Raised when a graph definition is invalid."""


class NodeExecutionError(OrchestratorError):
    """Raised when a node fails during execution."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RetryableError(NodeExecutionError):
    """Transient failure — safe to retry (timeouts, rate limits, API blips)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class NonRetryableError(NodeExecutionError):
    """Permanent failure — do not retry (validation, bad input)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class TimeoutError(RetryableError):
    """Node exceeded its configured timeout."""


class CheckpointPaused(OrchestratorError):
    """Signal that execution should pause at a human-in-the-loop checkpoint."""

    def __init__(self, message: str = "Waiting for human approval") -> None:
        super().__init__(message)
