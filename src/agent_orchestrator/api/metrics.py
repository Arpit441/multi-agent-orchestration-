"""Simple operational metrics derived from persisted runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_orchestrator.core.state import RunStatus


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def build_run_metrics(runs: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """
    Aggregate run counts, failure rate, and HITL wait stats.

    Each run dict should include at least: status, and optionally state / hitl_* fields.
    """
    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {s.value: 0 for s in RunStatus}
    total = len(runs)
    for run in runs:
        status = str(run.get("status") or "")
        if status in counts:
            counts[status] += 1
        else:
            counts[status] = counts.get(status, 0) + 1

    failed = counts.get(RunStatus.FAILED.value, 0)
    completed = counts.get(RunStatus.COMPLETED.value, 0)
    terminal = failed + completed
    failure_rate = (failed / terminal) if terminal else 0.0

    open_waits: list[float] = []
    resolved_waits: list[float] = []
    for run in runs:
        state = run.get("state") or {}
        paused_at = _parse_iso(state.get("hitl_paused_at") or run.get("hitl_paused_at"))
        resumed_at = _parse_iso(state.get("hitl_resumed_at") or run.get("hitl_resumed_at"))
        stored_wait = state.get("hitl_wait_seconds")
        status = str(run.get("status") or "")

        if status == RunStatus.PAUSED.value:
            # Prefer explicit pause timestamp; fall back to updated_at.
            start = paused_at or _parse_iso(run.get("updated_at"))
            wait = _seconds_between(start, now)
            if wait is not None:
                open_waits.append(wait)
        elif isinstance(stored_wait, (int, float)):
            resolved_waits.append(float(stored_wait))
        elif paused_at and resumed_at:
            wait = _seconds_between(paused_at, resumed_at)
            if wait is not None:
                resolved_waits.append(wait)

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    return {
        "runs_total": total,
        "runs_by_status": counts,
        "failures": failed,
        "failure_rate": round(failure_rate, 4),
        "hitl": {
            "currently_waiting": counts.get(RunStatus.PAUSED.value, 0),
            "open_wait_seconds_avg": _avg(open_waits),
            "open_wait_seconds_max": round(max(open_waits), 2) if open_waits else None,
            "resolved_wait_seconds_avg": _avg(resolved_waits),
            "resolved_wait_seconds_max": round(max(resolved_waits), 2) if resolved_waits else None,
            "resolved_sample_size": len(resolved_waits),
        },
    }
