"""Cost / path telemetry for the dashboard APIs."""

from agent_orchestrator.api.telemetry import (
    USD_PER_TOKEN,
    build_run_telemetry,
    list_item_telemetry,
)


def test_build_telemetry_basic_cost_and_bar():
    state = {
        "budget": {
            "tokens_used": 1240,
            "max_tokens_total": 8000,
            "started_at_ms": 1_000_000,
            "steps_taken": 2,
            "max_agent_steps": 5,
            "max_latency_ms": 30_000,
        },
        "plan_recommendation": "agentic_path",
        "execution_mode": "agentic",
        "circuit_breaker_triggered": False,
    }
    tel = build_run_telemetry(state, now_ms=1_000_000 + 14_000)
    assert tel["tokens_used"] == 1240
    assert tel["max_tokens_total"] == 8000
    assert tel["bar_level"] == "green"
    assert tel["path"] == "agentic_path"
    assert tel["path_label"] == "Agentic path"
    assert tel["latency_ms"] == 14_000
    assert abs(tel["estimated_cost_usd"] - round(1240 * USD_PER_TOKEN, 6)) < 1e-9
    assert abs(tel["estimated_cost_usd"] - 0.008) < 0.0001


def test_bar_levels_yellow_and_red():
    yellow = build_run_telemetry(
        {"budget": {"tokens_used": 5600, "max_tokens_total": 8000}}
    )
    red = build_run_telemetry(
        {"budget": {"tokens_used": 7500, "max_tokens_total": 8000}}
    )
    assert yellow["bar_level"] == "yellow"
    assert red["bar_level"] == "red"


def test_fast_path_from_recommendation_or_fallback():
    a = build_run_telemetry({"plan_recommendation": "fast_path", "budget": {}})
    b = build_run_telemetry(
        {"execution_mode": "fast_fallback", "circuit_breaker_triggered": True, "budget": {}}
    )
    assert a["path_label"] == "Fast path"
    assert b["path_label"] == "Fast path"
    assert b["circuit_breaker_triggered"] is True


def test_list_item_is_compact():
    item = list_item_telemetry(
        {
            "budget": {"tokens_used": 100, "max_tokens_total": 4000, "started_at_ms": 0},
            "plan_recommendation": "fast_path",
        },
        status="COMPLETED",
        updated_at="2026-01-01T00:00:10+00:00",
    )
    assert set(item) >= {
        "tokens_used",
        "max_tokens_total",
        "estimated_cost_usd",
        "latency_ms",
        "path",
        "path_label",
        "circuit_breaker_triggered",
        "bar_level",
    }
    assert "usd_per_token" not in item
