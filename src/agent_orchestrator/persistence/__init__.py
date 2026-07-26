"""SQLite persistence for runs, traces, and state snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_orchestrator.core.runner import PersistenceStore, RunRecord, TraceEvent
from agent_orchestrator.core.state import RunStatus, State


class SQLiteStore(PersistenceStore):
    """Durable store so crashed runs can resume from the last snapshot."""

    def __init__(self, db_path: str | Path = "data/orchestrator.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    graph_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_node TEXT,
                    state_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    step INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    node_name TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_run
                    ON snapshots(run_id, step);

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idem_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )

    async def save_run(self, run: RunRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, graph_name, status, current_node, state_json,
                    error, created_at, updated_at, step
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    current_node=excluded.current_node,
                    state_json=excluded.state_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    step=excluded.step
                """,
                (
                    run.run_id,
                    run.graph_name,
                    run.status.value,
                    run.current_node,
                    run.state.to_json(),
                    run.error,
                    run.created_at,
                    run.updated_at,
                    run.step,
                ),
            )
            # Replace trace events for simplicity and consistency.
            conn.execute("DELETE FROM trace_events WHERE run_id = ?", (run.run_id,))
            for seq, event in enumerate(run.trace):
                conn.execute(
                    """
                    INSERT INTO trace_events (run_id, seq, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (run.run_id, seq, json.dumps(event.to_dict(), default=str)),
                )

    async def load_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            trace_rows = conn.execute(
                "SELECT payload_json FROM trace_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()

        trace: list[TraceEvent] = []
        for tr in trace_rows:
            payload = json.loads(tr["payload_json"])
            trace.append(
                TraceEvent(
                    node_name=payload["node_name"],
                    attempt=payload["attempt"],
                    started_at=payload["started_at"],
                    ended_at=payload.get("ended_at"),
                    outcome=payload.get("outcome", "running"),
                    error=payload.get("error"),
                    retryable=payload.get("retryable"),
                    duration_ms=payload.get("duration_ms"),
                    input_snapshot=payload.get("input_snapshot") or {},
                    output_snapshot=payload.get("output_snapshot") or {},
                    state_diff=payload.get("state_diff") or {},
                )
            )

        return RunRecord(
            run_id=row["run_id"],
            graph_name=row["graph_name"],
            status=RunStatus(row["status"]),
            current_node=row["current_node"],
            state=State.from_json(row["state_json"]),
            trace=trace,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            step=row["step"],
        )

    async def save_snapshot(
        self, run_id: str, step: int, node_name: str, phase: str, state: State
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (run_id, step, node_name, phase, state_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, step, node_name, phase, state.to_json()),
            )

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, graph_name, status, current_node, error,
                       created_at, updated_at, step, state_json
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            state_json = item.pop("state_json", "{}")
            try:
                state = json.loads(state_json) if state_json else {}
            except json.JSONDecodeError:
                state = {}
            topic = (state.get("topic") or state.get("subject") or "").strip()
            item["title"] = topic or "Untitled task"
            item["graph_label"] = (
                "Support"
                if item.get("graph_name") == "support_resolution"
                else "Research"
            )
            out.append(item)
        return out

    async def latest_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM snapshots
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    async def get_idempotency(self, key: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT idem_key, request_hash, run_id, created_at
                FROM idempotency_keys
                WHERE idem_key = ?
                """,
                (key,),
            ).fetchone()
        return dict(row) if row else None

    async def put_idempotency(self, key: str, request_hash: str, run_id: str) -> None:
        from datetime import datetime, timezone

        created = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO idempotency_keys (idem_key, request_hash, run_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, request_hash, run_id, created),
            )

    async def list_runs_for_metrics(self, limit: int = 500) -> list[dict[str, Any]]:
        """Runs with state fields needed for HITL wait metrics."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, graph_name, status, current_node, error,
                       created_at, updated_at, step, state_json
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            state_json = item.pop("state_json", "{}")
            try:
                item["state"] = json.loads(state_json) if state_json else {}
            except json.JSONDecodeError:
                item["state"] = {}
            out.append(item)
        return out
