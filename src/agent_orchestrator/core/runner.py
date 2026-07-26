"""Execution engine: retries, timeouts, checkpoints, and state transitions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_orchestrator.core.errors import (
    CheckpointPaused,
    NodeExecutionError,
    NonRetryableError,
    RetryableError,
    TimeoutError as NodeTimeoutError,
)
from agent_orchestrator.core.graph import Graph
from agent_orchestrator.core.state import RunStatus, State, assert_transition


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TraceEvent:
    node_name: str
    attempt: int
    started_at: str
    ended_at: str | None = None
    outcome: str = "running"
    error: str | None = None
    retryable: bool | None = None
    duration_ms: float | None = None
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    output_snapshot: dict[str, Any] = field(default_factory=dict)
    state_diff: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "error": self.error,
            "retryable": self.retryable,
            "duration_ms": self.duration_ms,
            "input_snapshot": self.input_snapshot,
            "output_snapshot": self.output_snapshot,
            "state_diff": self.state_diff,
        }


@dataclass
class RunRecord:
    run_id: str
    graph_name: str
    status: RunStatus
    current_node: str | None
    state: State
    trace: list[TraceEvent] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())
    step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph_name": self.graph_name,
            "status": self.status.value,
            "current_node": self.current_node,
            "state": self.state.data,
            "trace": [t.to_dict() for t in self.trace],
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "step": self.step,
        }


class PersistenceStore:
    """Minimal persistence interface used by the runner."""

    async def save_run(self, run: RunRecord) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def load_run(self, run_id: str) -> RunRecord | None:  # pragma: no cover
        raise NotImplementedError

    async def save_snapshot(
        self, run_id: str, step: int, node_name: str, phase: str, state: State
    ) -> None:  # pragma: no cover
        raise NotImplementedError


class InMemoryStore(PersistenceStore):
    """Used in tests and when no SQLite path is configured."""

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.idempotency: dict[str, dict[str, str]] = {}

    async def save_run(self, run: RunRecord) -> None:
        self.runs[run.run_id] = run

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    async def save_snapshot(
        self, run_id: str, step: int, node_name: str, phase: str, state: State
    ) -> None:
        self.snapshots.append(
            {
                "run_id": run_id,
                "step": step,
                "node_name": node_name,
                "phase": phase,
                "state": state.clone().data,
            }
        )

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        runs = sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)
        return [
            {
                "run_id": r.run_id,
                "graph_name": r.graph_name,
                "status": r.status.value,
                "current_node": r.current_node,
                "error": r.error,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "step": r.step,
            }
            for r in runs[:limit]
        ]

    async def get_idempotency(self, key: str) -> dict[str, str] | None:
        return self.idempotency.get(key)

    async def put_idempotency(self, key: str, request_hash: str, run_id: str) -> None:
        from datetime import datetime, timezone

        self.idempotency[key] = {
            "idem_key": key,
            "request_hash": request_hash,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def list_runs_for_metrics(self, limit: int = 500) -> list[dict[str, Any]]:
        runs = sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)
        return [
            {
                "run_id": r.run_id,
                "graph_name": r.graph_name,
                "status": r.status.value,
                "current_node": r.current_node,
                "error": r.error,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "step": r.step,
                "state": dict(r.state.data),
            }
            for r in runs[:limit]
        ]


class GraphRunner:
    """Executes a compiled graph with retries, timeouts, and checkpoints."""

    def __init__(self, graph: Graph, store: PersistenceStore | None = None) -> None:
        self.graph = graph
        self.store: PersistenceStore = store or InMemoryStore()

    def _set_status(self, run: RunRecord, status: RunStatus) -> None:
        assert_transition(run.status, status)
        run.status = status
        run.updated_at = _utcnow().isoformat()

    async def create_run(self, initial_state: dict[str, Any] | State | None = None) -> RunRecord:
        if isinstance(initial_state, State):
            state = initial_state.clone()
        else:
            state = State(data=dict(initial_state or {}))
        run = RunRecord(
            run_id=str(uuid.uuid4()),
            graph_name=self.graph.name,
            status=RunStatus.PENDING,
            current_node=self.graph.entry_point,
            state=state,
        )
        await self.store.save_run(run)
        return run

    async def execute(
        self,
        initial_state: dict[str, Any] | State | None = None,
        *,
        run_id: str | None = None,
    ) -> RunRecord:
        if run_id:
            run = await self.store.load_run(run_id)
            if run is None:
                raise KeyError(f"Unknown run_id: {run_id}")
        else:
            run = await self.create_run(initial_state)

        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            return run

        if run.status == RunStatus.PENDING:
            self._set_status(run, RunStatus.RUNNING)
        elif run.status == RunStatus.PAUSED:
            self._set_status(run, RunStatus.RUNNING)
        elif run.status == RunStatus.RETRYING:
            self._set_status(run, RunStatus.RUNNING)

        await self.store.save_run(run)
        return await self._loop(run)

    async def approve(
        self,
        run_id: str,
        *,
        decision: str = "approve",
        edited_state: dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> RunRecord:
        run = await self.store.load_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        if run.status != RunStatus.PAUSED:
            raise RuntimeError(f"Run is not paused (status={run.status.value})")

        paused_at = run.state.get("hitl_paused_at")
        resumed_at = _utcnow().isoformat()
        run.state.set("hitl_resumed_at", resumed_at)
        if paused_at:
            try:
                start = datetime.fromisoformat(str(paused_at).replace("Z", "+00:00"))
                end = datetime.fromisoformat(resumed_at)
                run.state.set("hitl_wait_seconds", max(0.0, (end - start).total_seconds()))
            except ValueError:
                pass

        run.state.set("human_decision", decision)
        run.state.set("human_comment", comment)
        if edited_state:
            run.state.update(edited_state)
        run.state.set("checkpoint_resolved", True)
        run.state.set("awaiting_human", False)

        if decision == "reject":
            self._set_status(run, RunStatus.FAILED)
            run.error = comment or "Rejected by human"
            await self.store.save_run(run)
            return run

        if decision == "revise":
            note = (comment or "").strip()
            if not note:
                raise RuntimeError("Revision requires feedback in the comment field")
            human_revs = int(run.state.get("human_revision_count") or 0) + 1
            if human_revs > 5:
                raise RuntimeError("Too many human revision rounds (max 5)")
            target = self._revise_target(run)
            if target not in self.graph.nodes:
                raise RuntimeError(f"Cannot revise: target node '{target}' is not in the graph")

            # Dedicated field so the critic's later "feedback" cannot overwrite human notes
            # before the writer/specialist runs.
            run.state.set("human_feedback", note)
            run.state.set("feedback", note)
            run.state.set("approved", False)
            run.state.set("checkpoint_resolved", False)
            run.state.set("awaiting_human", False)
            run.state.set("pending_human_revision", True)
            run.state.set("human_revision_count", human_revs)
            run.state.set(
                "revision_count",
                int(run.state.get("revision_count") or 0) + 1,
            )
            # Keep prior draft available so the writer revises it instead of starting over.
            prior = (
                run.state.get("report")
                or run.state.get("draft_reply")
                or run.state.get("final_report")
                or ""
            )
            if prior:
                run.state.set("previous_draft", prior)
            run.current_node = target
            run.error = None
            await self.store.save_run(run)
            return await self.execute(run_id=run_id)

        # Approve path — clear revision flags.
        run.state.set("pending_human_revision", False)
        run.state.set("human_feedback", "")
        run.state.set("previous_draft", "")
        run.state.set("checkpoint_resolved", True)
        if run.current_node:
            nxt = self.graph.next_nodes(run.current_node, run.state)
            if not nxt:
                self._set_status(run, RunStatus.COMPLETED)
                run.current_node = None
                await self.store.save_run(run)
                return run
            run.current_node = nxt[0]

        # Persist HITL decision before continuing so crash mid-approve is recoverable.
        await self.store.save_run(run)
        return await self.execute(run_id=run_id)

    def _revise_target(self, run: RunRecord) -> str:
        """Prefer checkpoint config, then writer, then support specialist by intent."""
        node_name = run.current_node
        if node_name and node_name in self.graph.nodes:
            cfg = getattr(self.graph.nodes[node_name].instance, "config", {}) or {}
            configured = cfg.get("revise_to")
            if configured:
                return str(configured)
        if "writer" in self.graph.nodes:
            return "writer"
        intent = str(run.state.get("intent") or run.state.get("route_taken") or "").lower()
        mapping = {
            "faq": "faq_agent",
            "technical": "technical_agent",
            "billing": "billing_agent",
        }
        if intent in mapping and mapping[intent] in self.graph.nodes:
            return mapping[intent]
        for candidate in ("faq_agent", "frontline"):
            if candidate in self.graph.nodes:
                return candidate
        raise RuntimeError("No revision target node available in this graph")

    async def resume(self, run_id: str, state_updates: dict[str, Any] | None = None) -> RunRecord:
        run = await self.store.load_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        if state_updates:
            run.state.update(state_updates)
            await self.store.save_run(run)
        return await self.execute(run_id=run_id)

    async def _loop(self, run: RunRecord) -> RunRecord:
        while run.current_node and run.status == RunStatus.RUNNING:
            node_name = run.current_node
            if node_name not in self.graph.nodes:
                self._set_status(run, RunStatus.FAILED)
                run.error = f"Unknown node: {node_name}"
                await self.store.save_run(run)
                return run

            compiled = self.graph.nodes[node_name]
            policy = compiled.retry_policy

            try:
                run.state = await self._execute_node(run, compiled.instance, policy)
            except CheckpointPaused as exc:
                self._set_status(run, RunStatus.PAUSED)
                run.state.set("hitl_paused_at", run.updated_at)
                run.error = str(exc)
                await self.store.save_run(run)
                return run
            except NodeExecutionError as exc:
                fallback = self.graph.fallback_target(node_name)
                if fallback:
                    run.current_node = fallback
                    run.error = f"Node '{node_name}' failed after retries; fallback to '{fallback}': {exc}"
                    await self.store.save_run(run)
                    continue
                self._set_status(run, RunStatus.FAILED)
                run.error = str(exc)
                await self.store.save_run(run)
                return run

            # Success path — evaluate edges / terminals.
            if node_name in self.graph.terminal_nodes:
                # Still allow edges if present; otherwise complete.
                nxt = self.graph.next_nodes(node_name, run.state)
                if not nxt:
                    run.current_node = None
                    self._set_status(run, RunStatus.COMPLETED)
                    await self.store.save_run(run)
                    return run
                run.current_node = nxt[0] if len(nxt) == 1 else await self._fan_out(run, nxt)
                await self.store.save_run(run)
                continue

            nxt = self.graph.next_nodes(node_name, run.state)
            if not nxt:
                run.current_node = None
                self._set_status(run, RunStatus.COMPLETED)
                await self.store.save_run(run)
                return run

            if len(nxt) == 1:
                run.current_node = nxt[0]
            else:
                run.current_node = await self._fan_out(run, nxt)
            await self.store.save_run(run)

        if run.status == RunStatus.RUNNING and not run.current_node:
            self._set_status(run, RunStatus.COMPLETED)
            await self.store.save_run(run)
        return run

    async def _fan_out(self, run: RunRecord, node_names: list[str]) -> str | None:
        """Execute parallel branches, merge state, return join successor if any."""

        async def _branch(name: str) -> State:
            local = run.state.clone()
            compiled = self.graph.nodes[name]
            return await self._execute_node_isolated(run, name, compiled.instance, compiled.retry_policy, local)

        results = await asyncio.gather(*[_branch(n) for n in node_names], return_exceptions=True)
        merged = run.state.clone()
        for name, result in zip(node_names, results):
            if isinstance(result, Exception):
                raise result
            # Shallow-merge branch outputs under branch key + top-level updates.
            assert isinstance(result, State)
            merged.set(f"branch_{name}", result.data)
            for k, v in result.data.items():
                if k not in run.state.data or run.state.data.get(k) != v:
                    merged.set(k, v)
        run.state = merged

        # Join: if all parallel nodes share the same next target, continue there.
        successors: list[str] = []
        for name in node_names:
            nxt = self.graph.next_nodes(name, run.state)
            successors.extend(nxt)
        unique = list(dict.fromkeys(successors))
        return unique[0] if len(unique) == 1 else (unique[0] if unique else None)

    async def _execute_node_isolated(
        self,
        run: RunRecord,
        node_name: str,
        instance: Any,
        policy: Any,
        state: State,
    ) -> State:
        previous = run.current_node
        original_state = run.state
        run.current_node = node_name
        run.state = state
        try:
            return await self._execute_node(run, instance, policy)
        finally:
            run.state = original_state
            run.current_node = previous

    async def _execute_node(self, run: RunRecord, instance: Any, policy: Any) -> State:
        node_name = instance.name
        last_error: Exception | None = None

        for attempt in range(1, policy.max_attempts + 1):
            if attempt > 1:
                self._set_status(run, RunStatus.RETRYING)
                await self.store.save_run(run)
                delay = policy.delay_for_attempt(attempt)
                if delay:
                    await asyncio.sleep(delay)
                self._set_status(run, RunStatus.RUNNING)
                await self.store.save_run(run)

            run.step += 1
            before = run.state.clone()
            await self.store.save_snapshot(run.run_id, run.step, node_name, "before", before)

            event = TraceEvent(
                node_name=node_name,
                attempt=attempt,
                started_at=_utcnow().isoformat(),
                input_snapshot=before.data,
            )
            run.trace.append(event)
            await self.store.save_run(run)

            started = time.perf_counter()
            working = run.state.clone()
            try:
                coro = instance.run(working)
                if policy.timeout_seconds:
                    result_state = await asyncio.wait_for(coro, timeout=policy.timeout_seconds)
                else:
                    result_state = await coro
                if not isinstance(result_state, State):
                    raise NonRetryableError(
                        f"Node '{node_name}' returned {type(result_state)!r}, expected State"
                    )

                duration_ms = (time.perf_counter() - started) * 1000
                event.ended_at = _utcnow().isoformat()
                event.duration_ms = duration_ms
                event.outcome = "success"
                event.output_snapshot = result_state.data
                event.state_diff = before.diff(result_state)
                run.state = result_state
                await self.store.save_snapshot(
                    run.run_id, run.step, node_name, "after", result_state
                )
                await self.store.save_run(run)
                return result_state

            except CheckpointPaused:
                # Keep annotations written by the checkpoint node (message, preview).
                run.state = working
                duration_ms = (time.perf_counter() - started) * 1000
                event.ended_at = _utcnow().isoformat()
                event.duration_ms = duration_ms
                event.outcome = "paused"
                event.output_snapshot = run.state.data
                await self.store.save_snapshot(
                    run.run_id, run.step, node_name, "after", run.state
                )
                await self.store.save_run(run)
                raise

            except asyncio.TimeoutError as exc:
                last_error = NodeTimeoutError(
                    f"Node '{node_name}' timed out after {policy.timeout_seconds}s"
                )
                self._fail_event(event, started, last_error, retryable=True)
                await self.store.save_run(run)
                if attempt >= policy.max_attempts:
                    raise last_error from exc
                continue

            except NodeExecutionError as exc:
                last_error = exc
                self._fail_event(event, started, exc, retryable=exc.retryable)
                await self.store.save_run(run)
                if not exc.retryable or attempt >= policy.max_attempts:
                    raise
                continue

            except Exception as exc:  # noqa: BLE001 — classify unknown as retryable API blip
                last_error = RetryableError(f"Node '{node_name}' failed: {exc}")
                self._fail_event(event, started, last_error, retryable=True)
                await self.store.save_run(run)
                if attempt >= policy.max_attempts:
                    raise last_error from exc
                continue

        raise last_error or NodeExecutionError(f"Node '{node_name}' failed")

    @staticmethod
    def _fail_event(
        event: TraceEvent, started: float, error: Exception, *, retryable: bool
    ) -> None:
        event.ended_at = _utcnow().isoformat()
        event.duration_ms = (time.perf_counter() - started) * 1000
        event.outcome = "error"
        event.error = str(error)
        event.retryable = retryable
