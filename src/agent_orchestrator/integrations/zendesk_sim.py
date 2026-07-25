"""Simulated Zendesk connector — inbound tickets + outbound reply deliveries."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SAMPLE_TICKETS = [
    {
        "external_ticket_id": "ZD-48291",
        "subject": "Charged twice for Pro",
        "message": (
            "I've been charged twice for my Pro plan this month. Order #48291. "
            "This is the third time — I'm really frustrated and considering a chargeback."
        ),
        "customer_name": "Priya Sharma",
        "customer_plan": "Pro",
        "customer_email": "priya@acmelogistics.com",
        "status": "open",
        "priority": "high",
    },
    {
        "external_ticket_id": "ZD-48310",
        "subject": "Can't reset password",
        "message": (
            "The reset password link keeps saying it's expired. "
            "I need to log in before our team standup tomorrow."
        ),
        "customer_name": "Alex Chen",
        "customer_plan": "Team",
        "customer_email": "alex@example.com",
        "status": "open",
        "priority": "normal",
    },
    {
        "external_ticket_id": "ZD-48355",
        "subject": "Dashboard loading forever",
        "message": (
            "Since yesterday evening the analytics dashboard spins forever on Chrome. "
            "Windows 11. Started around 6pm IST."
        ),
        "customer_name": "Sam Rivera",
        "customer_plan": "Enterprise",
        "customer_email": "sam@rivera.io",
        "status": "open",
        "priority": "normal",
    },
]


class ZendeskSimulator:
    """In-app Zendesk stand-in with the same conceptual contract as a real connector."""

    def __init__(self, db_path: str | Path = "data/zendesk_sim.db") -> None:
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
                CREATE TABLE IF NOT EXISTS connection (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    connected INTEGER NOT NULL DEFAULT 0,
                    subdomain TEXT,
                    connected_at TEXT
                );
                INSERT OR IGNORE INTO connection (id, connected) VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS tickets (
                    external_ticket_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    message TEXT NOT NULL,
                    customer_name TEXT,
                    customer_plan TEXT,
                    customer_email TEXT,
                    status TEXT NOT NULL,
                    priority TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    external_ticket_id TEXT NOT NULL,
                    run_id TEXT,
                    reply_body TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM connection WHERE id = 1").fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'"
            ).fetchone()["c"]
        return {
            "provider": "zendesk",
            "mode": "simulator",
            "connected": bool(row["connected"]),
            "subdomain": row["subdomain"],
            "connected_at": row["connected_at"],
            "open_tickets": open_count,
        }

    def connect(self, subdomain: str = "acme-demo") -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE connection
                SET connected = 1, subdomain = ?, connected_at = ?
                WHERE id = 1
                """,
                (subdomain, _utcnow()),
            )
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'"
            ).fetchone()["c"]
            if open_count == 0:
                # Fresh connect (or all previous tickets solved) → load demo inbox.
                for t in SAMPLE_TICKETS:
                    self._insert_ticket(conn, t)
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE connection
                SET connected = 0, subdomain = NULL, connected_at = NULL
                WHERE id = 1
                """
            )
            # Clear demo data so the next Connect feels like a fresh integration.
            conn.execute("DELETE FROM deliveries")
            conn.execute("DELETE FROM tickets")
        return self.status()

    def _insert_ticket(self, conn: sqlite3.Connection, ticket: dict[str, Any]) -> None:
        payload = dict(ticket)
        payload["source"] = "zendesk"
        conn.execute(
            """
            INSERT OR REPLACE INTO tickets (
                external_ticket_id, subject, message, customer_name, customer_plan,
                customer_email, status, priority, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket["external_ticket_id"],
                ticket["subject"],
                ticket["message"],
                ticket.get("customer_name"),
                ticket.get("customer_plan"),
                ticket.get("customer_email"),
                ticket.get("status", "open"),
                ticket.get("priority", "normal"),
                json.dumps(payload),
                _utcnow(),
            ),
        )

    def list_tickets(self, *, open_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if open_only:
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE status = 'open' ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tickets ORDER BY created_at DESC"
                ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item.pop("payload_json", None)
            out.append(item)
        return out

    def get_ticket(self, external_ticket_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE external_ticket_id = ?",
                (external_ticket_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        payload = json.loads(data.pop("payload_json") or "{}")
        data["source"] = "zendesk"
        data.update({k: v for k, v in payload.items() if k not in data})
        return data

    def ingest_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Simulate Zendesk pushing a new ticket webhook."""
        ticket_id = payload.get("external_ticket_id") or f"ZD-{uuid.uuid4().hex[:5].upper()}"
        ticket = {
            "external_ticket_id": ticket_id,
            "subject": payload.get("subject") or "New ticket",
            "message": payload.get("message") or payload.get("description") or "",
            "customer_name": payload.get("customer_name") or "Customer",
            "customer_plan": payload.get("customer_plan") or "unknown",
            "customer_email": payload.get("customer_email"),
            "status": "open",
            "priority": payload.get("priority") or "normal",
        }
        with self._connect() as conn:
            if not self.status()["connected"]:
                conn.execute(
                    """
                    UPDATE connection SET connected = 1, subdomain = COALESCE(subdomain, 'acme-demo'),
                    connected_at = COALESCE(connected_at, ?) WHERE id = 1
                    """,
                    (_utcnow(),),
                )
            self._insert_ticket(conn, ticket)
        return self.get_ticket(ticket_id) or ticket

    def deliver_reply(
        self,
        *,
        external_ticket_id: str,
        reply_body: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        ticket = self.get_ticket(external_ticket_id)
        if ticket is None:
            raise KeyError(f"Unknown Zendesk ticket: {external_ticket_id}")
        if not self.status()["connected"]:
            raise RuntimeError("Zendesk simulator is not connected")

        delivery_id = str(uuid.uuid4())
        created = _utcnow()
        # Simulated successful Zendesk Comments API response
        detail = (
            f"POST /api/v2/tickets/{external_ticket_id}/comments "
            f"→ 201 Created (simulated)"
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, external_ticket_id, run_id, reply_body,
                    http_status, status, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    external_ticket_id,
                    run_id,
                    reply_body,
                    201,
                    "delivered",
                    detail,
                    created,
                ),
            )
            conn.execute(
                "UPDATE tickets SET status = 'solved' WHERE external_ticket_id = ?",
                (external_ticket_id,),
            )
        return {
            "delivery_id": delivery_id,
            "external_ticket_id": external_ticket_id,
            "run_id": run_id,
            "http_status": 201,
            "status": "delivered",
            "detail": detail,
            "created_at": created,
            "provider": "zendesk",
            "mode": "simulator",
        }

    def list_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM deliveries
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


_sim: ZendeskSimulator | None = None


def get_zendesk_sim(db_path: str | Path | None = None) -> ZendeskSimulator:
    global _sim
    if _sim is None:
        _sim = ZendeskSimulator(db_path or "data/zendesk_sim.db")
    return _sim


def set_zendesk_sim(sim: ZendeskSimulator | None) -> None:
    global _sim
    _sim = sim
