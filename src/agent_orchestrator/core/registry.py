"""Plugin registry for node types."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_orchestrator.core.errors import OrchestratorError
from agent_orchestrator.core.graph import BaseNode
from agent_orchestrator.core.policies import RetryPolicy

NodeFactory = Callable[[str, dict[str, Any], RetryPolicy], BaseNode]


class NodeRegistry:
    """Registration mechanism so new node types can be added without core changes."""

    def __init__(self) -> None:
        self._factories: dict[str, NodeFactory] = {}

    def register(self, node_type: str, factory: NodeFactory) -> None:
        self._factories[node_type] = factory

    def get(self, node_type: str) -> NodeFactory:
        if node_type not in self._factories:
            raise OrchestratorError(
                f"Unknown node type '{node_type}'. Registered: {sorted(self._factories)}"
            )
        return self._factories[node_type]

    def has(self, node_type: str) -> bool:
        return node_type in self._factories

    def types(self) -> list[str]:
        return sorted(self._factories)


_GLOBAL_REGISTRY = NodeRegistry()


def get_registry() -> NodeRegistry:
    return _GLOBAL_REGISTRY


def register_node(node_type: str) -> Callable[[type], type]:
    """Decorator: ``@register_node('llm_agent')`` on a node class with matching ctor."""

    def decorator(cls: type) -> type:
        def factory(name: str, config: dict[str, Any], retry_policy: RetryPolicy) -> BaseNode:
            return cls(name=name, config=config, retry_policy=retry_policy)  # type: ignore[call-arg]

        get_registry().register(node_type, factory)
        cls.node_type = node_type  # type: ignore[attr-defined]
        return cls

    return decorator
