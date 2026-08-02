"""Built-in LLM agent node powered by Gemini (optional bounded tool-calling)."""

from __future__ import annotations

import json
from typing import Any

from agent_orchestrator.core.budget import BudgetTracker
from agent_orchestrator.core.errors import NonRetryableError, RetryableError
from agent_orchestrator.core.policies import RetryPolicy
from agent_orchestrator.core.registry import register_node
from agent_orchestrator.core.state import State
from agent_orchestrator.llm import get_gemini_client
from agent_orchestrator.nodes.agent_tools import (
    Tool,
    invoke_tool,
    observation_text,
    tools_prompt_block,
)
from agent_orchestrator.nodes.thinking import (
    extract_thinking_from_payload,
    split_thinking_text,
    thinking_system_suffix,
)


@register_node("llm_agent")
class LLMAgentNode:
    """Calls Gemini with a system prompt and a user prompt template over state.

    When ``config["tools"]`` is a non-empty list of :class:`Tool`, runs a bounded
    ReAct loop: LLM → parse tool_call → execute → append observation → repeat.
    Without tools, behaves as a single LLM call (legacy path).
    """

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
        self.tools: list[Tool] = list(config.get("tools") or [])
        self.max_tool_iterations: int = int(config.get("max_tool_iterations", 3))
        self.max_tool_calls: int = int(config.get("max_tool_calls", 3))
        self.tool_trace_key: str = str(config.get("tool_trace_key") or f"{name}_trace")

    def _tools_by_name(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}

    def _render_user(self, state: State) -> str:
        ctx = {**state.data, "state": state.to_json()}
        template = self.user_template
        # Safe substitution: only replace known {placeholders}, ignore braces in values.
        try:
            for key, value in ctx.items():
                template = template.replace(
                    "{" + str(key) + "}", str(value if value is not None else "")
                )
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

    def _apply_json_payload(self, state: State, payload: Any) -> None:
        state.set(self.output_key, payload)
        if not isinstance(payload, dict):
            return
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
            "research_brief",
            "search_queries",
        )
        extra = tuple(self.config.get("flatten_keys") or ())
        for key in (*default_flatten, *extra):
            if key in payload:
                state.set(key, payload[key])
        if payload.get("escalate") is True:
            state.set("force_escalate", True)
        if self.config.get("track_revisions") and not payload.get("approved", False):
            state.set(
                "revision_count",
                int(state.get("revision_count") or 0) + 1,
            )
        if self.config.get("set_route"):
            state.set("route_taken", self.config["set_route"])
        if payload.get("draft_reply"):
            state.set("report", payload["draft_reply"])
        elif payload.get("preliminary_reply") and not state.get("draft_reply"):
            state.set("report", payload["preliminary_reply"])
            state.set("draft_reply", payload["preliminary_reply"])

    def _budget_system_suffix(self, remaining_calls: int, remaining_iters: int) -> str:
        names = ", ".join(t.name for t in self.tools)
        force = ""
        if remaining_calls <= 1 and "synthesize_findings" in self._tools_by_name():
            force = (
                " BUDGET CRITICAL: you have at most ONE tool call left. "
                "You MUST call synthesize_findings now (no search_web or browse_url)."
            )
        return (
            f"\n\nTool budget: {remaining_calls} tool call(s) remaining "
            f"(hard max {self.max_tool_calls}). "
            f"Loop iterations left: {remaining_iters}. "
            f"Tools: {names}.{force}"
        )

    async def _force_synthesize(
        self,
        state: State,
        trace: list[dict[str, Any]],
        thought: str,
    ) -> dict[str, Any]:
        tool = self._tools_by_name()["synthesize_findings"]
        result = await invoke_tool(tool, {}, state)
        trace.append(
            {
                "thought": thought,
                "action": "synthesize_findings",
                "action_input": {},
                "observation": observation_text(result),
            }
        )
        state.set(self.tool_trace_key, trace)
        if isinstance(result, dict):
            return result
        return {"done": True, "result": result}

    async def _run_with_tools(self, state: State) -> State:
        client = get_gemini_client()
        base_user = self._render_user(state)
        transcript: list[str] = []
        trace: list[dict[str, Any]] = []
        tool_calls_used = 0
        final_payload: dict[str, Any] | None = None
        thoughts: list[str] = []

        max_iters = max(1, self.max_tool_iterations)
        max_calls = max(1, self.max_tool_calls)
        tools_by_name = self._tools_by_name()
        has_synthesize = "synthesize_findings" in tools_by_name

        for iteration in range(max_iters):
            remaining_calls = max_calls - tool_calls_used
            remaining_iters = max_iters - iteration

            # Budget tight → skip the LLM and synthesize immediately.
            if has_synthesize and remaining_calls <= 1 and not state.get("researcher_done"):
                final_payload = await self._force_synthesize(
                    state,
                    trace,
                    thought=(
                        "Tool budget is tight — forcing synthesize_findings "
                        "to finish within the hard limit."
                    ),
                )
                tool_calls_used += 1
                BudgetTracker(state).record_step(1)
                break

            if remaining_calls <= 0:
                break

            # Run-level budget (tokens/latency/steps) — stop tool looping early.
            if not BudgetTracker(state).can_afford(0):
                if has_synthesize and not state.get("researcher_done"):
                    final_payload = await self._force_synthesize(
                        state,
                        trace,
                        thought="Run budget exhausted — synthesizing with evidence on hand.",
                    )
                    tool_calls_used += 1
                    BudgetTracker(state).record_step(1)
                break

            system = (
                f"{self.system_prompt}"
                f"{tools_prompt_block(self.tools)}"
                f"{self._budget_system_suffix(remaining_calls, remaining_iters)}"
            )
            if self.emit_thinking:
                system = f"{system}{thinking_system_suffix(json_mode=True)}"

            user = base_user
            if transcript:
                user = (
                    f"{base_user}\n\n--- ReAct transcript so far ---\n"
                    + "\n\n".join(transcript)
                    + "\n\nContinue. Return JSON only."
                )
            else:
                user = f"{user}\n\nReturn JSON only (tool_call or done)."

            try:
                payload, tokens = await client.generate_json(
                    system=system,
                    user=user,
                    temperature=self.temperature,
                )
                BudgetTracker(state).record_tokens(tokens)
            except NonRetryableError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if any(
                    x in msg
                    for x in ("rate", "quota", "timeout", "unavailable", "429", "503")
                ):
                    raise RetryableError(
                        f"Gemini transient error in '{self.name}': {exc}"
                    ) from exc
                raise RetryableError(f"Gemini error in '{self.name}': {exc}") from exc

            if not isinstance(payload, dict):
                payload = {"raw": payload}

            thinking = extract_thinking_from_payload(payload) or str(
                payload.get("thought") or ""
            )
            if thinking:
                thoughts.append(thinking)

            tool_call = payload.get("tool_call")
            done = bool(payload.get("done")) or (
                "research_brief" in payload and not tool_call
            )

            if isinstance(tool_call, dict) and tool_call.get("name"):
                name = str(tool_call.get("name"))
                arguments = tool_call.get("arguments") or {}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}

                tracker = BudgetTracker(state)
                # Refuse expensive tool work when the run budget cannot afford another step.
                if name != "synthesize_findings" and not tracker.can_afford(0):
                    if has_synthesize and not state.get("researcher_done"):
                        final_payload = await self._force_synthesize(
                            state,
                            trace,
                            thought=(
                                "BudgetTracker refused further tool spend — "
                                "forcing synthesize_findings."
                            ),
                        )
                        tool_calls_used += 1
                        BudgetTracker(state).record_step(1)
                        break
                    # Fall through to single-shot finalization.
                    tool_call = None
                else:
                    # Last slot must be synthesize when that tool exists.
                    if (
                        has_synthesize
                        and remaining_calls <= 1
                        and name != "synthesize_findings"
                    ):
                        final_payload = await self._force_synthesize(
                            state,
                            trace,
                            thought=(
                                f"Model requested '{name}' but only one call remains — "
                                "forcing synthesize_findings."
                            ),
                        )
                        tool_calls_used += 1
                        BudgetTracker(state).record_step(1)
                        break

                    tool = tools_by_name.get(name)
                    if tool is None:
                        obs = f"Unknown tool '{name}'. Choose from: {', '.join(tools_by_name)}"
                        trace.append(
                            {
                                "thought": thinking,
                                "action": name,
                                "action_input": arguments,
                                "observation": obs,
                            }
                        )
                        transcript.append(
                            f"Thought: {thinking}\nAction: {name} {json.dumps(arguments)}\n"
                            f"Observation: {obs}"
                        )
                        tool_calls_used += 1
                        BudgetTracker(state).record_step(1)
                        state.set(self.tool_trace_key, trace)
                        continue

                    try:
                        result = await invoke_tool(tool, arguments, state)
                        obs = observation_text(result)
                    except Exception as exc:  # noqa: BLE001
                        obs = f"Tool error: {exc}"
                        result = {"error": str(exc)}

                    tool_calls_used += 1
                    BudgetTracker(state).record_step(1)
                    trace.append(
                        {
                            "thought": thinking,
                            "action": name,
                            "action_input": arguments,
                            "observation": obs,
                        }
                    )
                    transcript.append(
                        f"Thought: {thinking}\n"
                        f"Action: {name} {json.dumps(arguments, default=str)}\n"
                        f"Observation: {obs}"
                    )
                    state.set(self.tool_trace_key, trace)

                    if name == "synthesize_findings" or (
                        isinstance(result, dict) and result.get("done")
                    ):
                        final_payload = result if isinstance(result, dict) else {"done": True}
                        break
                    continue

            # No tool_call — treat as final structured answer.
            clean = {
                k: v
                for k, v in payload.items()
                if k not in {"thinking", "tool_call"}
            }
            if thinking and "thought" not in clean:
                clean["thought"] = thinking
            final_payload = clean
            if thinking:
                trace.append(
                    {
                        "thought": thinking,
                        "action": None,
                        "action_input": None,
                        "observation": "final_answer",
                    }
                )
            break

        # Safety net: still no synthesis but synthesize tool exists.
        if (
            has_synthesize
            and not state.get("researcher_done")
            and final_payload is None
            and tool_calls_used < max_calls
        ):
            final_payload = await self._force_synthesize(
                state,
                trace,
                thought="Loop ended without a final answer — synthesizing findings.",
            )
        elif has_synthesize and not state.get("researcher_done") and final_payload is None:
            # No budget left — synthesize without counting (state packaging only).
            final_payload = await self._force_synthesize(
                state,
                trace,
                thought="No tool budget left — packaging whatever evidence was gathered.",
            )

        state.set(self.tool_trace_key, trace)
        if self.tool_trace_key == "research_trace":
            state.set("research_trace_text", json.dumps(trace, indent=2, default=str))
        if thoughts:
            self._store_thinking(state, "\n\n".join(thoughts))

        if final_payload is None:
            final_payload = {
                "research_brief": state.get("research_brief") or "",
                "search_queries": state.get("search_queries") or [],
                "done": True,
            }

        # Prefer state fields set by synthesize_findings.
        if state.get("research_plan"):
            plan = dict(state.get("research_plan") or {})
            plan.setdefault("research_trace_steps", len(trace))
            self._apply_json_payload(state, plan)
        else:
            self._apply_json_payload(state, final_payload)

        state.set("tool_calls_used", tool_calls_used)
        return state

    def _in_fast_fallback(self, state: State) -> bool:
        return (
            state.get("execution_mode") == "fast_fallback"
            or bool(state.get("circuit_breaker_triggered"))
        )

    async def _run_single_shot(self, state: State, *, fast_fallback: bool = False) -> State:
        """One LLM call — used for tool-less nodes and circuit-breaker fast_fallback."""
        client = get_gemini_client()
        user = self._render_user(state)
        system = self.system_prompt
        if fast_fallback:
            system = (
                f"{system}\n\nFAST FALLBACK MODE: Do not call tools. "
                "Return a complete single-shot answer now. If JSON is required, "
                "include all final fields (e.g. research_brief, search_queries) directly."
            )
        use_json = self.json_mode or (fast_fallback and bool(self.tools))
        if self.emit_thinking:
            system = f"{system}{thinking_system_suffix(json_mode=use_json)}"
        tracker = BudgetTracker(state)
        if use_json:
            payload, tokens = await client.generate_json(
                system=system,
                user=user,
                temperature=self.temperature,
            )
            tracker.record_tokens(tokens)
            if isinstance(payload, dict):
                thinking = extract_thinking_from_payload(payload)
                self._store_thinking(state, thinking)
                payload = {k: v for k, v in payload.items() if k != "thinking"}
                # Researcher fast path: package plan fields if present.
                if payload.get("research_brief") and not state.get("research_plan"):
                    state.set(
                        "research_plan",
                        {
                            "research_brief": payload.get("research_brief"),
                            "search_queries": payload.get("search_queries") or [],
                        },
                    )
            self._apply_json_payload(state, payload)
        else:
            result = await client.generate(
                system=system,
                user=user,
                temperature=self.temperature,
            )
            tracker.record_tokens(result.tokens_used)
            text = result.text
            if self.emit_thinking:
                thinking, text = split_thinking_text(text)
                self._store_thinking(state, thinking)
            state.set(self.output_key, text)
        return state

    async def run(self, state: State) -> State:
        from agent_orchestrator.api.workflow_config import feature_enabled

        fast = self._in_fast_fallback(state)
        react_ok = feature_enabled(state, "react_researcher", True)
        # Circuit breaker / feature flag: skip tool loops and answer in one shot.
        if self.tools and not fast and react_ok:
            try:
                await self._run_with_tools(state)
            except NonRetryableError:
                raise
            except RetryableError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if any(
                    x in msg
                    for x in ("rate", "quota", "timeout", "unavailable", "429", "503")
                ):
                    raise RetryableError(
                        f"Gemini transient error in '{self.name}': {exc}"
                    ) from exc
                raise RetryableError(f"Gemini error in '{self.name}': {exc}") from exc
            state.set("last_agent", self.name)
            return state

        try:
            await self._run_single_shot(state, fast_fallback=fast)
        except NonRetryableError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(x in msg for x in ("rate", "quota", "timeout", "unavailable", "429", "503")):
                raise RetryableError(f"Gemini transient error in '{self.name}': {exc}") from exc
            raise RetryableError(f"Gemini error in '{self.name}': {exc}") from exc
        state.set("last_agent", self.name)
        return state
