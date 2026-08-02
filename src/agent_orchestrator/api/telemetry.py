"""Budget / cost / path telemetry for the dashboard and run APIs."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

# Blended Gemini Flash-ish estimate for portfolio demos (~$6.50 / 1M tokens).
# 1,240 tokens ≈ $0.008 — matches the dashboard copy in the product brief.
USD_PER_TOKEN = 0.00000645

_TERMINAL = {"COMPLETED", "FAILED"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _path_from_state(state: dict[str, Any]) -> tuple[str, str]:
    rec = str(state.get("plan_recommendation") or "").strip().lower()
    mode = str(state.get("execution_mode") or "agentic").strip().lower()
    if rec == "fast_path" or mode == "fast_fallback":
        return "fast_path", "Fast path"
    if rec == "agentic_path":
        return "agentic_path", "Agentic path"
    if mode == "fast_fallback":
        return "fast_path", "Fast path"
    return "agentic_path", "Agentic path"


def _bar_level(ratio: float) -> str:
    if ratio >= 0.9:
        return "red"
    if ratio >= 0.7:
        return "yellow"
    return "green"


def build_run_telemetry(
    state: dict[str, Any] | None,
    *,
    status: str | None = None,
    updated_at: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Derive token/cost/latency/path summary from run state for the UI."""
    state = state if isinstance(state, dict) else {}
    budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}

    tokens_used = _as_int(budget.get("tokens_used"), 0)
    max_tokens = max(1, _as_int(budget.get("max_tokens_total"), 8000))
    steps_taken = _as_int(budget.get("steps_taken"), 0)
    max_steps = _as_int(budget.get("max_agent_steps"), 5)
    max_latency_ms = _as_int(budget.get("max_latency_ms"), 30_000)

    started = budget.get("started_at_ms")
    try:
        started_ms = int(started) if started is not None else None
    except (TypeError, ValueError):
        started_ms = None

    clock = now_ms if now_ms is not None else int(time.time() * 1000)
    end_ms = clock
    if status and str(status).upper() in _TERMINAL:
        ended = _iso_to_ms(updated_at)
        if ended is not None:
            end_ms = ended
    latency_ms = max(0, end_ms - started_ms) if started_ms is not None else 0

    ratio = tokens_used / max_tokens if max_tokens else 0.0
    path, path_label = _path_from_state(state)
    cost_usd = round(tokens_used * USD_PER_TOKEN, 6)
    breaker = bool(state.get("circuit_breaker_triggered"))

    return {
        "tokens_used": tokens_used,
        "max_tokens_total": max_tokens,
        "token_ratio": round(ratio, 4),
        "bar_level": _bar_level(ratio),
        "estimated_cost_usd": cost_usd,
        "latency_ms": int(latency_ms),
        "latency_seconds": round(latency_ms / 1000.0, 1),
        "path": path,
        "path_label": path_label,
        "plan_recommendation": state.get("plan_recommendation") or None,
        "execution_mode": str(state.get("execution_mode") or "agentic"),
        "circuit_breaker_triggered": breaker,
        "circuit_breaker_reason": state.get("circuit_breaker_reason"),
        "steps_taken": steps_taken,
        "max_agent_steps": max_steps,
        "max_latency_ms": max_latency_ms,
        "usd_per_token": USD_PER_TOKEN,
    }


def list_item_telemetry(
    state: dict[str, Any] | None,
    *,
    status: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Compact telemetry fields embedded in ``GET /api/runs`` list rows."""
    full = build_run_telemetry(state, status=status, updated_at=updated_at)
    return {
        "tokens_used": full["tokens_used"],
        "max_tokens_total": full["max_tokens_total"],
        "estimated_cost_usd": full["estimated_cost_usd"],
        "latency_ms": full["latency_ms"],
        "latency_seconds": full["latency_seconds"],
        "path": full["path"],
        "path_label": full["path_label"],
        "circuit_breaker_triggered": full["circuit_breaker_triggered"],
        "bar_level": full["bar_level"],
    }
