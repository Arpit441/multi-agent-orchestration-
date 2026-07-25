"""External app connectors (simulated + real-ready contracts)."""

from agent_orchestrator.integrations.zendesk_sim import (
    ZendeskSimulator,
    get_zendesk_sim,
    set_zendesk_sim,
)

__all__ = ["ZendeskSimulator", "get_zendesk_sim", "set_zendesk_sim"]
