"""Built-in LLM agent node powered by Gemini."""

from __future__ import annotations

from typing import Any

from agent_orchestrator.core.errors import NonRetryableError, RetryableError
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.state import State
from agent_orchestrator.llm import get_gemini_client
from agent_orchestrator.nodes.thinking import (
    extract_thinking_from_payload,
    split_thinking_text,
    thinking_system_suffix,
)


@register_node("llm_agent")
class LLMAgentNode:
    """Calls Gemini with a system prompt and a user prompt template over state."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy()
        self.system_prompt: str = config.get(
            "system_prompt", "You are a helpful assistant."
        )
        self.user_template: str = config.get(
            "user_template", "Continue the task using this state:\n{state}"
        )
        self.output_key: str = config.get("output_key", f"{name}_output")
        self.json_mode: bool = bool(config.get("json_mode", False))
        self.temperature: float = float(config.get("temperature", 0.4))
        self.emit_thinking: bool = bool(config.get("emit_thinking", True))

    def _render_user(self, state: State) -> str:
        ctx = {**state.data, "state": state.to_json()}
        template = self.user_template
        # Safe substitution: only replace known {placeholders}, ignore braces in values.
        try:
            for key, value in ctx.items():
                template = template.replace("{" + str(key) + "}", str(value if value is not None else ""))
            return template
        except Exception as exc:  # noqa: BLE001
            raise NonRetryableError(
                f"LLM node '{self.name}' failed to render template: {exc}"
            ) from exc

    def _store_thinking(self, state: State, thinking: str) -> None:
        text = (thinking or "").strip()
        if not text:
            return
        thoughts = dict(state.get("agent_thoughts") or {})
        thoughts[self.name] = text
        state.set("agent_thoughts", thoughts)
        state.set("last_thinking", text)
        state.set(f"{self.name}_thinking", text)

    async def run(self, state: State) -> State:
        client = get_gemini_client()
        user = self._render_user(state)
        system = self.system_prompt
        if self.emit_thinking:
            system = f"{system}{thinking_system_suffix(json_mode=self.json_mode)}"
        try:
            if self.json_mode:
                payload = await client.generate_json(
                    system=system,
                    user=user,
                    temperature=self.temperature,
                )
                if isinstance(payload, dict):
                    thinking = extract_thinking_from_payload(payload)
                    self._store_thinking(state, thinking)
                    # Keep tool/agent consumers free of the thinking metadata.
                    payload = {k: v for k, v in payload.items() if k != "thinking"}
                state.set(self.output_key, payload)
                if isinstance(payload, dict):
                    default_flatten = (
                        "approved",
                        "feedback",
                        "score",
                        "intent",
                        "summary",
                        "preliminary_reply",
                        "frustrated",
                        "urgency",
                        "sentiment_label",
                        "escalate",
                        "draft_reply",
                        "suggested_action",
                        "internal_notes",
                        "confidence",
                        "reason",
                    )
                    extra = tuple(self.config.get("flatten_keys") or ())
                    for key in (*default_flatten, *extra):
                        if key in payload:
                            state.set(key, payload[key])
                    # Map escalate from specialists/sentiment into a clear flag when true
                    if payload.get("escalate") is True:
                        state.set("force_escalate", True)
                    if self.config.get("track_revisions") and not payload.get(
                        "approved", False
                    ):
                        state.set(
                            "revision_count",
                            int(state.get("revision_count") or 0) + 1,
                        )
                    if self.config.get("set_route"):
                        state.set("route_taken", self.config["set_route"])
                    # Keep report/reply preview fields in sync for UI
                    if payload.get("draft_reply"):
                        state.set("report", payload["draft_reply"])
                    elif payload.get("preliminary_reply") and not state.get("draft_reply"):
                        state.set("report", payload["preliminary_reply"])
                        state.set("draft_reply", payload["preliminary_reply"])
            else:
                # One plain-text Gemini call. Long markdown reports break if forced
                # through JSON (unescaped newlines → Invalid control character).
                text = await client.generate(
                    system=system,
                    user=user,
                    temperature=self.temperature,
                )
                if self.emit_thinking:
                    thinking, text = split_thinking_text(text)
                    self._store_thinking(state, thinking)
                state.set(self.output_key, text)
        except NonRetryableError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(x in msg for x in ("rate", "quota", "timeout", "unavailable", "429", "503")):
                raise RetryableError(f"Gemini transient error in '{self.name}': {exc}") from exc
            raise RetryableError(f"Gemini error in '{self.name}': {exc}") from exc
        state.set("last_agent", self.name)
        return state
