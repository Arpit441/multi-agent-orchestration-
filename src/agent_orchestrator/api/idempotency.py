"""Idempotency helpers for start-run requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint_payload(payload: dict[str, Any]) -> str:
    """Stable hash of a start-run request body (excluding the key itself)."""
    cleaned = {k: v for k, v in payload.items() if k != "idempotency_key" and v is not None}
    raw = json.dumps(cleaned, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
