"""Gemini LLM client wrapper."""

from __future__ import annotations

import json
import os
import re
from typing import Any


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

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def generate(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.4,
    ) -> str:
        import asyncio

        def _call() -> str:
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
            return text.strip()

        return await asyncio.to_thread(_call)

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        text = await self.generate(
            system=system + "\n\nRespond with valid JSON only. No markdown fences.",
            user=user,
            temperature=temperature,
        )
        return extract_json(text)


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"Could not parse JSON from model response: {text[:200]}")


_default_client: GeminiClient | None = None


def get_gemini_client() -> GeminiClient:
    global _default_client
    if _default_client is None:
        _default_client = GeminiClient()
    return _default_client


def set_gemini_client(client: GeminiClient | None) -> None:
    global _default_client
    _default_client = client
