"""Cloud.ru Foundation Models catalog with TTL cache.

Used by the summarizer to discover each model's `max_model_len` window so
the chunker can pack inputs that just fit, and by the UI to populate the
model dropdown. Both consumers share a single cached API response.

Fail-soft: if the catalog endpoint is unreachable, callers get
`DEFAULT_MODEL_CONTEXT` or an empty model list back.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import logfire

from bankiru.config import get_settings

_TTL_SECONDS = 60 * 60  # 1 hour


@dataclass
class _CatalogCache:
    expires_at: float
    contexts: dict[str, int]
    entries: list[dict] = field(default_factory=list)


_cache: _CatalogCache | None = None
_cache_lock = asyncio.Lock()


async def _fetch_catalog() -> tuple[dict[str, int], list[dict]]:
    """Fetch the /models endpoint; return (contexts_map, raw_entries)."""
    s = get_settings()
    if not s.OPENAI_API_KEY:
        return {}, []
    url = s.OPENAI_BASE_URL.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {s.OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    entries = payload.get("data", [])
    contexts: dict[str, int] = {}
    for entry in entries:
        name = entry.get("id")
        ctx = entry.get("max_model_len")
        if name and isinstance(ctx, int):
            contexts[name] = ctx
    return contexts, entries


async def _ensure_cache() -> _CatalogCache:
    """Return a valid cache entry, refreshing if expired."""
    global _cache
    default = get_settings().DEFAULT_MODEL_CONTEXT

    now = time.monotonic()
    if _cache is None or _cache.expires_at <= now:
        async with _cache_lock:
            if _cache is None or _cache.expires_at <= now:
                try:
                    contexts, entries = await _fetch_catalog()
                    _cache = _CatalogCache(
                        expires_at=now + _TTL_SECONDS,
                        contexts=contexts,
                        entries=entries,
                    )
                except Exception as exc:
                    logfire.warning(
                        "model catalog fetch failed, using default {default}",
                        default=default, exc=str(exc),
                    )
                    _cache = _CatalogCache(expires_at=now + 60, contexts={})

    return _cache


async def get_model_context(model_name: str) -> int:
    """Return `max_model_len` for `model_name`, with TTL caching and fallback."""
    cache = await _ensure_cache()
    return cache.contexts.get(model_name, get_settings().DEFAULT_MODEL_CONTEXT)


async def list_llm_models(min_context: int = 0) -> list[str]:
    """List model names whose metadata.type is 'llm' (filtered to ``min_context`` if >0)."""
    cache = await _ensure_cache()
    names: list[str] = []
    for entry in cache.entries:
        if entry.get("metadata", {}).get("type") != "llm":
            continue
        if min_context and entry.get("max_model_len", 0) < min_context:
            continue
        if name := entry.get("id"):
            names.append(name)
    return sorted(names)
