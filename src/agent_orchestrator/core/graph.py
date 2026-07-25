"""Node, Edge, and Graph definitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_orchestrator.core.errors import GraphValidationError
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.state import State

ConditionFn = Callable[[State], bool]
NodeFactory = Callable[[str, dict[str, Any], RetryPolicy], "BaseNode"]


@runtime_checkable
class BaseNode(Protocol):
    """Minimal interface every node type must implement."""

    name: str
    retry_policy: RetryPolicy

    async def run(self, state: State) -> State:
        """Execute the node and return the (possibly mutated) state."""
        ...


@dataclass
class NodeSpec:
    """Declarative node definition used when building a graph."""

    name: str
    node_type: str
    config: dict[str, Any] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class Edge:
    """Control-flow edge. Conditional edges use ``condition``; fan-out uses parallel targets."""

    source: str
    target: str
    condition: ConditionFn | None = None
    label: str | None = None
    # When True, this edge participates in a fan-out group from ``source``.
    parallel: bool = False
    # Fallback edges are taken when a node exhausts retries.
    is_fallback: bool = False


@dataclass
class CompiledNode:
    name: str
    node_type: str
    instance: BaseNode
    retry_policy: RetryPolicy


@dataclass
class Graph:
    """Compiled directed graph ready for execution."""

    name: str
    entry_point: str
    nodes: dict[str, CompiledNode]
    edges: list[Edge]
    terminal_nodes: set[str] = field(default_factory=set)

    def outgoing(self, node_name: str, *, fallback: bool = False) -> list[Edge]:
        return [
            e
            for e in self.edges
            if e.source == node_name and e.is_fallback == fallback
        ]

    def next_nodes(self, node_name: str, state: State) -> list[str]:
        """Evaluate outgoing edges and return the next node name(s)."""
        edges = self.outgoing(node_name, fallback=False)
        if not edges:
            return []

        # Prefer conditional matches; unconditional edges always match.
        matched: list[Edge] = []
        for edge in edges:
            if edge.condition is None or edge.condition(state):
                matched.append(edge)

        if not matched:
            # If only conditional edges exist and none match, treat as terminal.
            return []

        # Fan-out: all parallel edges that matched, else first matched edge.
        parallel = [e for e in matched if e.parallel]
        if parallel:
            return [e.target for e in parallel]
        return [matched[0].target]

    def fallback_target(self, node_name: str) -> str | None:
        policy = self.nodes[node_name].retry_policy
        if policy.fallback_node:
            return policy.fallback_node
        fallbacks = self.outgoing(node_name, fallback=True)
        return fallbacks[0].target if fallbacks else None


class GraphBuilder:
    """Fluent builder that compiles a validated Graph."""

    def __init__(self, name: str, *, registry: Any | None = None) -> None:
        self.name = name
        self._registry = registry
        self._node_specs: dict[str, NodeSpec] = {}
        self._edges: list[Edge] = []
        self._entry: str | None = None
        self._terminals: set[str] = set()

    def add_node(
        self,
        name: str,
        node_type: str,
        *,
        config: dict[str, Any] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> GraphBuilder:
        if name in self._node_specs:
            raise GraphValidationError(f"Duplicate node name: {name}")
        self._node_specs[name] = NodeSpec(
            name=name,
            node_type=node_type,
            config=config or {},
            retry_policy=retry_policy or RetryPolicy(),
        )
        return self

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        condition: ConditionFn | None = None,
        label: str | None = None,
        parallel: bool = False,
        is_fallback: bool = False,
    ) -> GraphBuilder:
        self._edges.append(
            Edge(
                source=source,
                target=target,
                condition=condition,
                label=label,
                parallel=parallel,
                is_fallback=is_fallback,
            )
        )
        return self

    def set_entry(self, name: str) -> GraphBuilder:
        self._entry = name
        return self

    def mark_terminal(self, *names: str) -> GraphBuilder:
        self._terminals.update(names)
        return self

    def compile(self) -> Graph:
        from agent_orchestrator.core.registry import get_registry

        registry = self._registry or get_registry()

        if not self._entry:
            raise GraphValidationError("Graph entry point is not set")
        if self._entry not in self._node_specs:
            raise GraphValidationError(f"Entry point '{self._entry}' is not a node")

        for edge in self._edges:
            if edge.source not in self._node_specs:
                raise GraphValidationError(
                    f"Edge source '{edge.source}' is not a registered node"
                )
            if edge.target not in self._node_specs:
                raise GraphValidationError(
                    f"Edge target '{edge.target}' is not a registered node"
                )

        compiled: dict[str, CompiledNode] = {}
        for spec in self._node_specs.values():
            factory = registry.get(spec.node_type)
            instance = factory(spec.name, spec.config, spec.retry_policy)
            compiled[spec.name] = CompiledNode(
                name=spec.name,
                node_type=spec.node_type,
                instance=instance,
                retry_policy=spec.retry_policy,
            )

        # Reachability from entry (ignore fallback edges for reachability check).
        reachable: set[str] = set()
        stack = [self._entry]
        adj: dict[str, list[str]] = {n: [] for n in self._node_specs}
        for edge in self._edges:
            if not edge.is_fallback:
                adj[edge.source].append(edge.target)
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(adj.get(current, []))

        unreachable = set(self._node_specs) - reachable
        # Fallback-only targets may be unreachable via normal edges — allow them.
        fallback_targets = {e.target for e in self._edges if e.is_fallback}
        unreachable -= fallback_targets
        if unreachable:
            raise GraphValidationError(
                f"Unreachable nodes from entry '{self._entry}': {sorted(unreachable)}"
            )

        terminals = set(self._terminals)
        # Implicit terminals: nodes with no non-fallback outgoing edges.
        for name in self._node_specs:
            outs = [e for e in self._edges if e.source == name and not e.is_fallback]
            if not outs:
                terminals.add(name)

        return Graph(
            name=self.name,
            entry_point=self._entry,
            nodes=compiled,
            edges=list(self._edges),
            terminal_nodes=terminals,
        )
