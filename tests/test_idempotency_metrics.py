"""Idempotency + metrics tests."""

from datetime import datetime, timedelta, timezone

import pytest

from agent_orchestrator.api.idempotency import fingerprint_payload
from agent_orchestrator.api.metrics import build_run_metrics
from agent_orchestrator.core.runner import InMemoryStore


def test_fingerprint_stable_and_ignores_key():
    a = fingerprint_payload({"graph": "research_report", "topic": "edge AI", "idempotency_key": "k1"})
    b = fingerprint_payload({"topic": "edge AI", "graph": "research_report", "idempotency_key": "k2"})
    c = fingerprint_payload({"graph": "research_report", "topic": "other"})
    assert a == b
    assert a != c


@pytest.mark.asyncio
async def test_idempotency_store_roundtrip():
    store = InMemoryStore()
    await store.put_idempotency("key-1", "hash-a", "run-1")
    row = await store.get_idempotency("key-1")
    assert row["run_id"] == "run-1"
    assert row["request_hash"] == "hash-a"
    assert await store.get_idempotency("missing") is None


def test_metrics_counts_failures_and_hitl_wait():
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    paused_at = (now - timedelta(seconds=90)).isoformat()
    resumed_at = (now - timedelta(seconds=30)).isoformat()
    runs = [
        {"status": "COMPLETED", "state": {"hitl_wait_seconds": 45.0}},
        {"status": "FAILED", "state": {}},
        {
            "status": "PAUSED",
            "updated_at": paused_at,
            "state": {"hitl_paused_at": paused_at},
        },
        {
            "status": "COMPLETED",
            "state": {
                "hitl_paused_at": paused_at,
                "hitl_resumed_at": resumed_at,
                "hitl_wait_seconds": 60.0,
            },
        },
    ]
    metrics = build_run_metrics(runs, now=now)
    assert metrics["runs_total"] == 4
    assert metrics["runs_by_status"]["COMPLETED"] == 2
    assert metrics["runs_by_status"]["FAILED"] == 1
    assert metrics["runs_by_status"]["PAUSED"] == 1
    assert metrics["failures"] == 1
    assert metrics["failure_rate"] == round(1 / 3, 4)
    assert metrics["hitl"]["currently_waiting"] == 1
    assert metrics["hitl"]["open_wait_seconds_avg"] == 90.0
    assert metrics["hitl"]["resolved_wait_seconds_avg"] == 52.5
