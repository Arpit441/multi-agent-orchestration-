"""Helpers for extracting short agent thinking blocks from model output."""

from __future__ import annotations

import re
from typing import Any


_THINKING_BLOCK = re.compile(
    r"<<<THINKING>>>\s*(.*?)\s*<<<END_THINKING>>>",
    re.IGNORECASE | re.DOTALL,
)

# Fallback: opening marker present but the model forgot the closing marker.
_THINKING_OPEN_ONLY = re.compile(
    r"<<<THINKING>>>\s*(.*?)(?:\n\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Fallback: model used a plain "Thinking:" / "Reasoning:" lead-in instead.
_THINKING_PREFIX = re.compile(
    r"^\s*(?:\*{0,2})(?:thinking|reasoning)(?:\*{0,2})\s*[:\-]\s*(.*?)(?:\n\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def split_thinking_text(text: str) -> tuple[str, str]:
    """Return (thinking, answer). Tolerant of missing/renamed markers."""
    raw = text or ""

    match = _THINKING_BLOCK.search(raw)
    if match:
        thinking = match.group(1).strip()
        answer = (_THINKING_BLOCK.sub("", raw, count=1)).strip()
        return thinking, answer

    match = _THINKING_OPEN_ONLY.search(raw)
    if match and "<<<thinking>>>" in raw.lower():
        thinking = match.group(1).strip()
        answer = raw[match.end():].strip()
        return thinking, answer

    match = _THINKING_PREFIX.match(raw)
    if match:
        thinking = match.group(1).strip()
        answer = raw[match.end():].strip()
        # Only treat as thinking if a real answer remains after it.
        if answer:
            return thinking, answer

    return "", raw.strip()


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
            "ONE short sentence of reasoning (max ~25 words). "
            "Write thinking as a single line: no line breaks, no unescaped quotes, "
            "no markdown."
        )
    return (
        "\n\nBefore your answer, write a short reasoning block exactly in this format:\n"
        "<<<THINKING>>>\n"
        "2-4 sentences of what you will do and why.\n"
        "<<<END_THINKING>>>\n"
        "Then provide your full normal answer after that block (markdown/text as usual). "
        "Do not wrap the whole answer in JSON."
    )
