"""TTL in-memory cache helpers."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from cachetools import TTLCache

T = TypeVar("T")

# Shared process-local caches (fine for single-instance deploy).
graph_meta_cache: TTLCache = TTLCache(maxsize=8, ttl=300)  # 5 minutes
search_cache: TTLCache = TTLCache(maxsize=64, ttl=600)  # 10 minutes
health_cache: TTLCache = TTLCache(maxsize=1, ttl=10)  # 10 seconds


def cached_get(cache: TTLCache, key: str, factory: Callable[[], T]) -> T:
    if key in cache:
        return cache[key]  # type: ignore[return-value]
    value = factory()
    cache[key] = value
    return value


def cache_stats() -> dict[str, Any]:
    return {
        "graph_meta": {"size": len(graph_meta_cache), "ttl_seconds": 300},
        "search": {"size": len(search_cache), "ttl_seconds": 600},
        "health": {"size": len(health_cache), "ttl_seconds": 10},
    }
