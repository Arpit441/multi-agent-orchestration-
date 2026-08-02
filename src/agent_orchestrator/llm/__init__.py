"""Gemini LLM client wrapper."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class GenerateResult:
    """Text response plus usage accounting for the budget tracker."""

    text: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _estimate_tokens(text: str) -> int:
    # Rough fallback when the SDK omits usage_metadata (~4 chars / token).
    return max(1, len(text) // 4)


def _usage_from_response(response: Any, prompt_text: str, output_text: str) -> tuple[int, int, int]:
    meta = getattr(response, "usage_metadata", None)
    prompt_tokens = 0
    completion_tokens = 0
    total = 0
    if meta is not None:
        prompt_tokens = int(
            getattr(meta, "prompt_token_count", None)
            or getattr(meta, "input_token_count", None)
            or 0
        )
        completion_tokens = int(
            getattr(meta, "candidates_token_count", None)
            or getattr(meta, "output_token_count", None)
            or 0
        )
        total = int(getattr(meta, "total_token_count", None) or 0)
        if not total:
            total = prompt_tokens + completion_tokens
    if total <= 0:
        total = _estimate_tokens(prompt_text) + _estimate_tokens(output_text)
        if prompt_tokens <= 0:
            prompt_tokens = _estimate_tokens(prompt_text)
        if completion_tokens <= 0:
            completion_tokens = _estimate_tokens(output_text)
    return total, prompt_tokens, completion_tokens


class GeminiClient:
    """Thin wrapper around the Google GenAI SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Add it to your environment or .env file."
                )
            from google import genai
            from google.genai import types

            # Prevent hung planner/agent calls from blocking forever (ms).
            http_timeout_ms = int(os.getenv("GEMINI_HTTP_TIMEOUT_MS", "45000"))
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=http_timeout_ms),
            )
        return self._client

    async def generate(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.4,
    ) -> GenerateResult:
        import asyncio

        def _call() -> GenerateResult:
            client = self._get_client()
            prompt = f"{system.strip()}\n\n---\n\n{user.strip()}"
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "temperature": temperature,
                },
            )
            text = getattr(response, "text", None)
            if not text:
                # Fallback for SDK response shapes
                try:
                    text = response.candidates[0].content.parts[0].text
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"Empty Gemini response: {exc}") from exc
            text = text.strip()
            total, prompt_tokens, completion_tokens = _usage_from_response(
                response, prompt, text
            )
            return GenerateResult(
                text=text,
                tokens_used=total,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return await asyncio.to_thread(_call)

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> tuple[dict[str, Any], int]:
        result = await self.generate(
            system=system + "\n\nRespond with valid JSON only. No markdown fences.",
            user=user,
            temperature=temperature,
        )
        return extract_json(result.text), result.tokens_used


def _sanitize_json_control_chars(text: str) -> str:
    """Escape raw control characters that LLMs often leave inside JSON strings."""

    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ord(ch) < 0x20:
            out.append({
                "\n": "\\n",
                "\r": "\\r",
                "\t": "\\t",
                "\b": "\\b",
                "\f": "\\f",
            }.get(ch, f"\\u{ord(ch):04x}"))
            continue
        out.append(ch)
    return "".join(out)


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    last_error: Exception | None = None
    for candidate in candidates:
        for variant in (candidate, _sanitize_json_control_chars(candidate)):
            try:
                payload = json.loads(variant)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError as exc:
                last_error = exc
                continue

    salvaged = _salvage_json_object(text)
    if salvaged:
        return salvaged

    raise ValueError(
        f"Could not parse JSON from model response: {text[:200]}"
        + (f" ({last_error})" if last_error else "")
    )


_STRING_KEYS = (
    "thinking",
    "research_brief",
    "feedback",
    "summary",
    "preliminary_reply",
    "draft_reply",
    "intent",
    "suggested_action",
    "sentiment_label",
    "urgency",
    "reason",
)


def _loose_string_field(text: str, key: str) -> str | None:
    """Pull a JSON string field even when surrounding JSON is broken."""
    # "key": " ... until unescaped quote or end of text
    pattern = re.compile(
        rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)',
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    raw = match.group(1)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return (
            raw.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _salvage_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort field extraction when json.loads fails (truncated / bad quotes)."""
    out: dict[str, Any] = {}
    for key in _STRING_KEYS:
        value = _loose_string_field(text, key)
        if value is not None:
            out[key] = value

    queries = re.search(r'"search_queries"\s*:\s*(\[[^\]]*\])', text, re.DOTALL)
    if queries:
        try:
            parsed = json.loads(_sanitize_json_control_chars(queries.group(1)))
            if isinstance(parsed, list):
                out["search_queries"] = [str(q) for q in parsed]
        except json.JSONDecodeError:
            loose = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', queries.group(1))
            if loose:
                out["search_queries"] = loose

    score = re.search(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', text)
    if score:
        try:
            out["score"] = float(score.group(1))
            if out["score"].is_integer():
                out["score"] = int(out["score"])
        except ValueError:
            pass

    approved = re.search(r'"approved"\s*:\s*(true|false)', text, re.IGNORECASE)
    if approved:
        out["approved"] = approved.group(1).lower() == "true"

    escalate = re.search(r'"escalate"\s*:\s*(true|false)', text, re.IGNORECASE)
    if escalate:
        out["escalate"] = escalate.group(1).lower() == "true"

    # Need at least one useful field beyond thinking alone.
    useful = {k for k in out if k != "thinking"}
    if not useful:
        return None
    return out


_default_client: GeminiClient | None = None


def get_gemini_client() -> GeminiClient:
    global _default_client
    if _default_client is None:
        _default_client = GeminiClient()
    return _default_client


def set_gemini_client(client: GeminiClient | None) -> None:
    global _default_client
    _default_client = client
