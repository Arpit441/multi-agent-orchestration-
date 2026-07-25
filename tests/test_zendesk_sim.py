"""Zendesk simulator connector tests."""

from agent_orchestrator.integrations.zendesk_sim import ZendeskSimulator


def test_connect_seed_deliver(tmp_path):
    sim = ZendeskSimulator(tmp_path / "zd.db")
    assert sim.status()["connected"] is False

    st = sim.connect("acme-demo")
    assert st["connected"] is True
    assert st["open_tickets"] == 3

    tickets = sim.list_tickets()
    assert len(tickets) == 3
    tid = tickets[0]["external_ticket_id"]

    delivery = sim.deliver_reply(
        external_ticket_id=tid,
        reply_body="We've refunded the duplicate charge.",
        run_id="run-1",
    )
    assert delivery["http_status"] == 201
    assert delivery["status"] == "delivered"
    assert delivery["mode"] == "simulator"

    updated = sim.get_ticket(tid)
    assert updated["status"] == "solved"
    assert len(sim.list_deliveries()) == 1

    sim.disconnect()
    assert sim.status()["connected"] is False
    assert sim.list_tickets(open_only=False) == []
    assert sim.list_deliveries() == []

    # Reconnect reseeds sample inbound tickets.
    st2 = sim.connect("acme-demo")
    assert st2["connected"] is True
    assert st2["open_tickets"] == 3
    assert len(sim.list_tickets()) == 3


def test_webhook_ingest(tmp_path):
    sim = ZendeskSimulator(tmp_path / "zd2.db")
    ticket = sim.ingest_webhook(
        {
            "subject": "API timeout",
            "message": "Getting 504s on /v1/export for the last hour.",
            "customer_name": "Jordan",
            "customer_plan": "Enterprise",
        }
    )
    assert ticket["external_ticket_id"].startswith("ZD-")
    assert sim.status()["connected"] is True
    assert sim.get_ticket(ticket["external_ticket_id"])["subject"] == "API timeout"
