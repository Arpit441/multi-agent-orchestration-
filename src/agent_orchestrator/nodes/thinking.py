"""Helpers for extracting short agent thinking blocks from model output."""

from __future__ import annotations

import re
from typing import Any


_THINKING_BLOCK = re.compile(
    r"<<<THINKING>>>\s*(.*?)\s*<<<END_THINKING>>>",
    re.IGNORECASE | re.DOTALL,
)


def split_thinking_text(text: str) -> tuple[str, str]:
    """Return (thinking, answer). If no markers, thinking is empty."""
    raw = text or ""
    match = _THINKING_BLOCK.search(raw)
    if not match:
        return "", raw.strip()
    thinking = match.group(1).strip()
    answer = (_THINKING_BLOCK.sub("", raw, count=1)).strip()
    return thinking, answer


def extract_thinking_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("thinking")
        return str(value).strip() if value is not None else ""
    return ""


def thinking_system_suffix(*, json_mode: bool) -> str:
    """Instructions appended to the SAME model call (never a second API request)."""
    if json_mode:
        return (
            "\n\nAlso include a top-level JSON string field named \"thinking\" with "
            "2-4 sentences of your reasoning before the final decision/output. "
            "Keep it concrete and specific to this task. Keep the thinking string "
            "on one or two lines without raw control characters."
        )
    return (
        "\n\nBefore your answer, write a short reasoning block exactly in this format:\n"
        "<<<THINKING>>>\n"
        "2-4 sentences of what you will do and why.\n"
        "<<<END_THINKING>>>\n"
        "Then provide your full normal answer after that block (markdown/text as usual). "
        "Do not wrap the whole answer in JSON."
    )
