"""Customer Support Resolution Network — multi-specialist ticket routing."""

from __future__ import annotations

from agent_orchestrator.core.graph import Graph, GraphBuilder
from agent_orchestrator.core.policies import BackoffStrategy, RetryPolicy
from agent_orchestrator.core.state import State

import agent_orchestrator.nodes  # noqa: F401
from agent_orchestrator.nodes.tool_node import register_tool

# Shared company policy snippets specialists can use (demo knowledge base).
SUPPORT_POLICY = """
Company support policy (demo):
- Duplicate charges: refund the extra charge within 1–2 business days if Order ID is verified.
- Password reset: guide customer to Settings → Security → Reset password; never ask for password in chat.
- Outages: acknowledge, share status page link, offer follow-up when resolved.
- Angry / VIP / legal threats: escalate to a human agent before promising compensation above $50.
- Always be empathetic, concise, and specific about next steps.
""".strip()


async def deliver_ticket_tool(state: State, config: dict) -> dict:
    reply = (
        state.get("draft_reply")
        or state.get("specialist_reply")
        or state.get("preliminary_reply")
        or ""
    )
    result: dict = {
        "final_reply": reply,
        "final_report": reply,  # reuse report UI tab
        "delivered": True,
        "suggested_action": state.get("suggested_action") or "none",
        "resolution_route": state.get("route_taken") or state.get("intent") or "unknown",
        "delivery_note": config.get(
            "note", "Support reply finalized after specialist routing / human sign-off."
        ),
    }

    # Outbound to simulated Zendesk when this run was sourced from a ticket.
    source = str(state.get("source") or "").lower()
    external_id = state.get("external_ticket_id")
    if source == "zendesk" and external_id and reply:
        from agent_orchestrator.integrations import get_zendesk_sim

        try:
            outbound = get_zendesk_sim().deliver_reply(
                external_ticket_id=str(external_id),
                reply_body=str(reply),
                run_id=state.get("orchestrator_run_id"),
            )
            result["zendesk_delivery"] = outbound
            result["delivery_note"] = (
                f"Reply sent to Zendesk ticket {external_id} "
                f"(simulated · HTTP {outbound.get('http_status')})."
            )
        except Exception as exc:  # noqa: BLE001
            result["zendesk_delivery"] = {
                "status": "failed",
                "error": str(exc),
                "external_ticket_id": str(external_id),
            }
            result["delivery_note"] = f"Local delivery ok; Zendesk outbound failed: {exc}"

    return result


# Unique tool name so it doesn't clash with research deliver.
register_tool("deliver_ticket", deliver_ticket_tool)


def _intent(state: State) -> str:
    intent = state.get("intent")
    if isinstance(intent, str):
        return intent.lower().strip()
    frontline = state.get("frontline_output")
    if isinstance(frontline, dict) and frontline.get("intent"):
        return str(frontline["intent"]).lower().strip()
    return "faq"


def _revision_count(state: State) -> int:
    try:
        return int(state.get("revision_count") or 0)
    except (TypeError, ValueError):
        return 0


def _needs_escalation(state: State) -> bool:
    if state.get("force_escalate"):
        return True
    if state.get("frustrated") is True:
        return True
    urgency = str(state.get("urgency") or "").lower()
    if urgency in {"high", "critical"}:
        return True
    sentiment = state.get("sentiment_output")
    if isinstance(sentiment, dict):
        if sentiment.get("frustrated") is True:
            return True
        if str(sentiment.get("urgency") or "").lower() in {"high", "critical"}:
            return True
        if sentiment.get("escalate") is True:
            return True
    frontline = state.get("frontline_output")
    if isinstance(frontline, dict) and frontline.get("escalate") is True:
        return True
    return False


def _no_escalation(state: State) -> bool:
    return not _needs_escalation(state)


def _is_faq(state: State) -> bool:
    return _no_escalation(state) and _intent(state) in {"faq", "general", "account"}


def _is_technical(state: State) -> bool:
    return _no_escalation(state) and _intent(state) in {"technical", "bug", "outage", "product"}


def _is_billing(state: State) -> bool:
    return _no_escalation(state) and _intent(state) in {"billing", "payment", "refund", "invoice"}


def _critic_ok(state: State) -> bool:
    if _revision_count(state) >= 2:
        return True
    approved = state.get("approved")
    if isinstance(approved, bool):
        return approved
    critic = state.get("quality_output")
    if isinstance(critic, dict) and "approved" in critic:
        return bool(critic["approved"])
    return False


def _critic_needs_revision(state: State) -> bool:
    return not _critic_ok(state)


def _route_faq_revision(state: State) -> bool:
    return _critic_needs_revision(state) and _intent(state) in {"faq", "general", "account"}


def _route_tech_revision(state: State) -> bool:
    return _critic_needs_revision(state) and _intent(state) in {
        "technical",
        "bug",
        "outage",
        "product",
    }


def _route_billing_revision(state: State) -> bool:
    return _critic_needs_revision(state) and _intent(state) in {
        "billing",
        "payment",
        "refund",
        "invoice",
    }


def build_support_resolution_graph() -> Graph:
    """
    frontline → sentiment →
      |-- escalate → human_escalate → deliver
      |-- faq → faq_agent → quality_critic → (revise loop | deliver)
      |-- technical → technical_agent → …
      |-- billing → billing_agent → …
    """
    builder = GraphBuilder("support_resolution")
    llm_retry = RetryPolicy(
        max_attempts=3,
        backoff=BackoffStrategy.EXPONENTIAL,
        timeout_seconds=90,
    )

    builder.add_node(
        "knowledge_lookup",
        "tool",
        config={"tool": "knowledge_lookup", "top_k": 5},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=15),
    )

    builder.add_node(
        "frontline",
        "llm_agent",
        config={
            "system_prompt": (
                "You are the frontline customer-support agent. Classify the ticket and "
                "draft a short preliminary reply grounded in the organisation knowledge "
                "context when provided. Return JSON only with keys: "
                "intent (one of: faq, technical, billing), "
                "summary (string), "
                "preliminary_reply (string), "
                "escalate (boolean — true if legal threat, VIP demand, or clearly needs human), "
                "confidence (0-1)."
            ),
            "user_template": (
                "Customer: {customer_name}\n"
                "Plan: {customer_plan}\n"
                "Subject: {subject}\n"
                "Message:\n{message}\n\n"
                "Organisation knowledge (uploaded docs — follow these for tone/policy):\n"
                "{knowledge_context}\n\n"
                "Built-in policy notes:\n" + SUPPORT_POLICY + "\n\n"
                "Return JSON only."
            ),
            "output_key": "frontline_output",
            "json_mode": True,
            "temperature": 0.2,
        },
        retry_policy=llm_retry,
    )

    builder.add_node(
        "sentiment",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a sentiment and risk agent for support tickets. "
                "Return JSON only with keys: "
                "frustrated (boolean), sentiment_label (calm|annoyed|frustrated|angry), "
                "urgency (low|medium|high|critical), "
                "escalate (boolean — true if customer is angry, threatens cancel/legal, or repeated complaint), "
                "reason (short string)."
            ),
            "user_template": (
                "Subject: {subject}\nMessage:\n{message}\n\n"
                "Frontline summary:\n{summary}\n\n"
                "Return JSON only."
            ),
            "output_key": "sentiment_output",
            "json_mode": True,
            "temperature": 0.1,
        },
        retry_policy=llm_retry,
    )

    specialist_system = (
        "You are a specialist support agent. Prefer the organisation knowledge context "
        "and policy notes over inventing rules. Match the company's tone if described. "
        "Write a clear, empathetic customer-facing reply and a machine suggested_action. "
        "Return JSON with keys: draft_reply (string), suggested_action (string), "
        "internal_notes (string)."
    )

    specialist_user = (
        "Customer: {customer_name}\nPlan: {customer_plan}\n"
        "Subject: {subject}\nMessage:\n{message}\n\n"
        "Frontline summary: {summary}\n"
        "Preliminary reply: {preliminary_reply}\n"
        "Revision feedback: {feedback}\n\n"
        "Organisation knowledge (uploaded docs):\n{knowledge_context}\n\n"
        "Built-in policy:\n" + SUPPORT_POLICY + "\n\nReturn JSON only."
    )

    builder.add_node(
        "faq_agent",
        "llm_agent",
        config={
            "system_prompt": specialist_system
            + " You handle FAQs and account how-tos.",
            "user_template": "Intent: faq\n" + specialist_user,
            "output_key": "specialist_output",
            "json_mode": True,
            "temperature": 0.3,
            "set_route": "faq",
        },
        retry_policy=llm_retry,
    )

    builder.add_node(
        "technical_agent",
        "llm_agent",
        config={
            "system_prompt": specialist_system
            + " You diagnose technical issues and outages; include troubleshooting steps.",
            "user_template": "Intent: technical\n" + specialist_user,
            "output_key": "specialist_output",
            "json_mode": True,
            "temperature": 0.3,
            "set_route": "technical",
        },
        retry_policy=llm_retry,
    )

    builder.add_node(
        "billing_agent",
        "llm_agent",
        config={
            "system_prompt": specialist_system
            + " You handle billing, refunds, and invoices; never invent charge amounts.",
            "user_template": "Intent: billing\n" + specialist_user,
            "output_key": "specialist_output",
            "json_mode": True,
            "temperature": 0.3,
            "set_route": "billing",
        },
        retry_policy=llm_retry,
    )

    builder.add_node(
        "quality_critic",
        "llm_agent",
        config={
            "system_prompt": (
                "You are a support quality critic. Check empathy, clarity, next steps, "
                "and alignment with the organisation knowledge / policy. "
                "Reject replies that invent policy or ignore uploaded guidelines. "
                "Return JSON: approved (boolean), score (0-10), feedback (string). "
                "Approve only if score >= 7."
            ),
            "user_template": (
                "Customer message:\n{message}\n\n"
                "Draft reply:\n{draft_reply}\n\n"
                "Suggested action: {suggested_action}\n\n"
                "Organisation knowledge:\n{knowledge_context}\n\n"
                "Built-in policy:\n" + SUPPORT_POLICY + "\n\nReturn JSON only."
            ),
            "output_key": "quality_output",
            "json_mode": True,
            "track_revisions": True,
            "temperature": 0.1,
        },
        retry_policy=llm_retry,
    )

    builder.add_node(
        "human_escalate",
        "checkpoint",
        config={
            "message": (
                "Escalation: review the draft before it goes to the customer "
                "(frustrated / high-risk ticket)."
            ),
            "preview_keys": [
                "subject",
                "message",
                "intent",
                "frustrated",
                "urgency",
                "draft_reply",
                "preliminary_reply",
                "suggested_action",
                "sentiment_output",
            ],
        },
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=None),
    )

    builder.add_node(
        "deliver",
        "tool",
        config={"tool": "deliver_ticket"},
        retry_policy=RetryPolicy(max_attempts=1, timeout_seconds=10),
    )

    builder.set_entry("knowledge_lookup")
    builder.add_edge("knowledge_lookup", "frontline")
    builder.add_edge("frontline", "sentiment")

    # After sentiment: escalate OR route to specialist
    builder.add_edge(
        "sentiment",
        "human_escalate",
        condition=_needs_escalation,
        label="escalate",
    )
    builder.add_edge("sentiment", "faq_agent", condition=_is_faq, label="faq")
    builder.add_edge(
        "sentiment", "technical_agent", condition=_is_technical, label="technical"
    )
    builder.add_edge(
        "sentiment", "billing_agent", condition=_is_billing, label="billing"
    )
    # Fallback if intent weird but not escalating: FAQ
    builder.add_edge(
        "sentiment",
        "faq_agent",
        condition=lambda s: _no_escalation(s)
        and not (_is_technical(s) or _is_billing(s) or _is_faq(s)),
        label="fallback_faq",
    )

    for specialist in ("faq_agent", "technical_agent", "billing_agent"):
        builder.add_edge(specialist, "quality_critic")

    builder.add_edge(
        "quality_critic", "deliver", condition=_critic_ok, label="approved"
    )
    builder.add_edge(
        "quality_critic", "faq_agent", condition=_route_faq_revision, label="revise_faq"
    )
    builder.add_edge(
        "quality_critic",
        "technical_agent",
        condition=_route_tech_revision,
        label="revise_tech",
    )
    builder.add_edge(
        "quality_critic",
        "billing_agent",
        condition=_route_billing_revision,
        label="revise_billing",
    )
    # If critic fails but intent unknown after max revisions path already forced ok;
    # still need a path — escalate leftover weak replies
    builder.add_edge(
        "quality_critic",
        "human_escalate",
        condition=lambda s: _critic_needs_revision(s)
        and not (
            _route_faq_revision(s) or _route_tech_revision(s) or _route_billing_revision(s)
        ),
        label="escalate_unknown",
    )

    builder.add_edge("human_escalate", "deliver")
    builder.mark_terminal("deliver")

    return builder.compile()


def default_support_state(
    *,
    subject: str,
    message: str,
    customer_name: str = "Customer",
    customer_plan: str = "unknown",
    customer_email: str | None = None,
    source: str = "manual",
    external_ticket_id: str | None = None,
) -> dict:
    state = {
        "subject": subject,
        "message": message,
        "customer_name": customer_name,
        "customer_plan": customer_plan,
        "customer_email": customer_email or "",
        "source": source,
        "external_ticket_id": external_ticket_id,
        "topic": subject,  # for UI title reuse
        "intent": "",
        "summary": "",
        "preliminary_reply": "",
        "draft_reply": "",
        "suggested_action": "",
        "feedback": "",
        "frustrated": False,
        "urgency": "medium",
        "approved": False,
        "revision_count": 0,
        "report": "",
        "knowledge_context": "",
    }
    return state
