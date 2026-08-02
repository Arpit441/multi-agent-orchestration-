"""Per-workflow default config, UI overrides, and hard ceilings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

# Hard ceilings — overrides cannot push past these.
CONFIG_CEILINGS: dict[str, int] = {
    "max_tokens_total": 50_000,
    "max_latency_ms": 300_000,
    "max_agent_steps": 50,
}


@dataclass
class BudgetConfig:
    max_tokens_total: int = 8000
    max_latency_ms: int = 120_000
    max_agent_steps: int = 5


@dataclass
class FeatureFlags:
    dynamic_planning: bool = True
    react_researcher: bool = True
    fact_check_critic: bool = True
    debate_loop: bool = False
    cross_run_memory: bool = False
    force_human_review: bool = False


@dataclass
class WorkflowConfig:
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    features: FeatureFlags = field(default_factory=FeatureFlags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": asdict(self.budget),
            "features": asdict(self.features),
        }


@dataclass
class Workflow:
    """Registered workflow metadata + hardcoded defaults."""

    id: str
    label: str
    description: str
    input_mode: str
    default_config: WorkflowConfig

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "input_mode": self.input_mode,
            "default_config": self.default_config.to_dict(),
            "ui": _ui_hints(self.id),
        }


def _ui_hints(workflow_id: str) -> dict[str, Any]:
    if workflow_id == "research_report":
        return {
            "advanced_toggle": {
                "id": "deep_research",
                "label": "Deep Research Mode",
                "description": "Enables critic debate loops and raises the token budget.",
                "default": False,
                "estimate_default": {
                    "tokens": 8000,
                    "time_ms": 60_000,
                    "label": "~8k tokens · ~60s",
                },
                "estimate_enabled": {
                    "tokens": 15000,
                    "time_ms": 90_000,
                    "label": "~15k tokens · ~90s",
                },
            }
        }
    if workflow_id == "support_resolution":
        return {
            "advanced_toggle": {
                "id": "force_human_review",
                "label": "Force Human Review",
                "description": "Always pause for approval before sending the reply.",
                "default": False,
                "estimate_default": {
                    "tokens": 4000,
                    "time_ms": 20_000,
                    "label": "~4k tokens · ~20s",
                },
                "estimate_enabled": {
                    "tokens": 4000,
                    "time_ms": 25_000,
                    "label": "~4k tokens · ~25s + review wait",
                },
            }
        }
    return {}


RESEARCH_DEFAULT = WorkflowConfig(
    budget=BudgetConfig(max_tokens_total=8000, max_latency_ms=120_000, max_agent_steps=5),
    features=FeatureFlags(
        dynamic_planning=True,
        react_researcher=True,
        fact_check_critic=True,
        debate_loop=False,
        cross_run_memory=False,
        force_human_review=False,
    ),
)

SUPPORT_DEFAULT = WorkflowConfig(
    budget=BudgetConfig(max_tokens_total=4000, max_latency_ms=90_000, max_agent_steps=5),
    features=FeatureFlags(
        dynamic_planning=False,
        react_researcher=False,
        fact_check_critic=False,
        debate_loop=False,
        cross_run_memory=False,
        force_human_review=False,
    ),
)

WORKFLOWS: dict[str, Workflow] = {
    "research_report": Workflow(
        id="research_report",
        label="Research report",
        description="Topic → cited, critically reviewed report with human sign-off.",
        input_mode="research",
        default_config=RESEARCH_DEFAULT,
    ),
    "support_resolution": Workflow(
        id="support_resolution",
        label="Customer Support Resolution Network",
        description="Frontline → sentiment → FAQ / Technical / Billing specialists → escalate if needed.",
        input_mode="support",
        default_config=SUPPORT_DEFAULT,
    ),
}


def get_workflow(workflow_id: str) -> Workflow | None:
    return WORKFLOWS.get(workflow_id)


def list_workflows() -> list[dict[str, Any]]:
    return [w.to_public_dict() for w in WORKFLOWS.values()]


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Recursively merge override into a copy of base (override wins)."""
    out = deepcopy(base)
    if not override:
        return out
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _clamp_int(value: Any, *, default: int, ceiling: int, floor: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(floor, min(n, ceiling))


def apply_ceilings(config: dict[str, Any]) -> dict[str, Any]:
    """Enforce hard ceilings on budget fields."""
    out = deepcopy(config)
    budget = dict(out.get("budget") or {})
    budget["max_tokens_total"] = _clamp_int(
        budget.get("max_tokens_total"),
        default=8000,
        ceiling=CONFIG_CEILINGS["max_tokens_total"],
    )
    budget["max_latency_ms"] = _clamp_int(
        budget.get("max_latency_ms"),
        default=30_000,
        ceiling=CONFIG_CEILINGS["max_latency_ms"],
        floor=1000,
    )
    budget["max_agent_steps"] = _clamp_int(
        budget.get("max_agent_steps"),
        default=5,
        ceiling=CONFIG_CEILINGS["max_agent_steps"],
    )
    out["budget"] = budget
    features = dict(out.get("features") or {})
    for key in (
        "dynamic_planning",
        "react_researcher",
        "fact_check_critic",
        "debate_loop",
        "cross_run_memory",
        "force_human_review",
    ):
        if key in features:
            features[key] = bool(features[key])
    out["features"] = features
    return out


def resolve_workflow_config(
    workflow_id: str,
    config_override: dict[str, Any] | None = None,
    *,
    ui_preset: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Merge defaults + optional override (+ UI presets). Returns (config, source).

    ``source`` is ``\"default\"`` or ``\"override\"``.
    """
    workflow = get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Unknown workflow: {workflow_id}")

    base = workflow.default_config.to_dict()
    user_override = dict(config_override or {})
    preset = ui_preset or user_override.pop("ui_preset", None)

    # Presets first, then explicit UI/API override wins (e.g. custom token budget).
    preset_patch: dict[str, Any] = {}
    if preset == "deep_research" and workflow_id == "research_report":
        preset_patch = {
            "budget": {"max_tokens_total": 15_000, "max_agent_steps": 8},
            "features": {
                "debate_loop": True,
                "dynamic_planning": True,
                "react_researcher": True,
                "fact_check_critic": True,
            },
        }
    elif preset == "force_human_review" and workflow_id == "support_resolution":
        preset_patch = {"features": {"force_human_review": True}}

    override = deep_merge(preset_patch, user_override) if (preset_patch or user_override) else {}
    merged = apply_ceilings(deep_merge(base, override if override else None))
    source = "override" if (config_override or preset) else "default"
    merged["ui_preset"] = preset
    return merged, source


def feature_enabled(state: dict[str, Any] | Any, name: str, default: bool = False) -> bool:
    """Read a feature flag from run state (dict or State-like)."""
    getter = state.get if hasattr(state, "get") else lambda k, d=None: None
    cfg = getter("workflow_config") or {}
    if not isinstance(cfg, dict):
        return default
    feats = cfg.get("features") or {}
    if not isinstance(feats, dict) or name not in feats:
        return default
    return bool(feats.get(name))


def apply_config_to_state(state: dict[str, Any], config: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Write resolved config into initial run state (budget + features)."""
    out = dict(state)
    cfg = apply_ceilings(config)
    budget = dict(out.get("budget") or {})
    budget.update(cfg.get("budget") or {})
    out["budget"] = budget
    out["workflow_config"] = cfg
    out["config_source"] = source  # "default" | "override"
    out["config_ui_preset"] = cfg.get("ui_preset")
    # Prompt helpers for planner
    out["budget_max_tokens"] = budget.get("max_tokens_total")
    out["budget_max_latency_ms"] = budget.get("max_latency_ms")
    out["budget_max_agent_steps"] = budget.get("max_agent_steps")
    out["budget_tokens_used"] = budget.get("tokens_used", 0)
    out["budget_token_soft_cap"] = int(int(budget.get("max_tokens_total") or 8000) * 0.8)
    return out
