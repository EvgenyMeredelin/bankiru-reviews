"""Cloud.ru Foundation Models catalog with TTL cache.

This module fetches and caches the list of available LLM models from the
Cloud.ru Foundation Models API (OpenAI-compatible ``/models`` endpoint).
It serves two consumers:

  1. **Summarizer** (summarizer.py) — needs each model's ``max_model_len``
     (context window size in tokens) to calculate how many review texts can
     fit in a single LLM call. Without this, the chunker would either
     overflow the context or underutilize it.

  2. **UI** (ui/foundation_models.py) — needs the list of available model
     names to populate the "Cloud model" dropdown in the Gradio interface.

Both consumers share a single cached API response to avoid redundant
network calls.

Fail-soft design: if the catalog endpoint is unreachable (network error,
auth failure, etc.), callers get ``DEFAULT_MODEL_CONTEXT`` (a conservative
fallback) or an empty model list. The API never crashes due to a catalog
fetch failure.

The cache uses a simple TTL (time-to-live) strategy with a lock to prevent
thundering-herd problems when multiple concurrent requests hit an expired
cache simultaneously.

Connection to other modules:
  - bankiru.api.summarizer       — calls get_model_context() to size chunks
  - bankiru.ui.foundation_models — calls list_llm_models() for the dropdown
  - bankiru.config               — provides OPENAI_API_KEY, OPENAI_BASE_URL,
                                   and DEFAULT_MODEL_CONTEXT fallback
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import logfire

from bankiru.config import get_settings

# Cache TTL: 1 hour. Model catalogs change infrequently, so a long TTL
# is appropriate. On failure, the cache is set to expire after 60 seconds
# to allow quick recovery without hammering the endpoint.
_TTL_SECONDS = 60 * 60  # 1 hour


@dataclass
class _CatalogCache:
    """Internal cache entry holding the fetched catalog data.

    Attributes:
        expires_at: monotonic timestamp when this cache entry becomes stale.
        contexts: mapping of model_name -> max_model_len (context window).
        entries: raw JSON entries from the /models response (used by
                 list_llm_models to filter by metadata.type).
    """
    expires_at: float
    contexts: dict[str, int]
    entries: list[dict] = field(default_factory=list)


# Module-level cache singleton and lock. The lock prevents multiple
# concurrent requests from fetching the catalog simultaneously when
# the cache expires (thundering herd prevention).
_cache: _CatalogCache | None = None
_cache_lock = asyncio.Lock()


async def _fetch_catalog() -> tuple[dict[str, int], list[dict]]:
    """Fetch the /models endpoint; return (contexts_map, raw_entries).

    Makes a single HTTP GET to the OpenAI-compatible /models endpoint.
    Parses the response to extract model names and their context window
    sizes (max_model_len).

    Returns:
        A tuple of:
          - contexts: dict mapping model name to context window size
          - entries: raw list of model entry dicts from the API response
    """
    s = get_settings()
    # If no API key is configured, return empty results (fail-soft).
    if not s.OPENAI_API_KEY:
        return {}, []
    # Build the /models URL from the base URL (strip trailing slash to
    # avoid double-slash in the URL).
    url = s.OPENAI_BASE_URL.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {s.OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    # Extract model entries from the standard OpenAI /models response format.
    entries = payload.get("data", [])
    # Build the contexts map: model_name -> max_model_len.
    # Only include entries that have both a valid name and an integer context size.
    contexts: dict[str, int] = {}
    for entry in entries:
        name = entry.get("id")
        ctx = entry.get("max_model_len")
        if name and isinstance(ctx, int):
            contexts[name] = ctx
    return contexts, entries


async def _ensure_cache() -> _CatalogCache:
    """Return a valid cache entry, refreshing if expired.

    Uses double-checked locking: first check without the lock (fast path),
    then re-check inside the lock (prevents duplicate fetches). On failure,
    creates a short-lived cache entry (60s TTL) with empty data so the
    system degrades gracefully.
    """
    global _cache
    default = get_settings().DEFAULT_MODEL_CONTEXT

    now = time.monotonic()
    # Fast path: cache is valid, return immediately without locking.
    if _cache is None or _cache.expires_at <= now:
        async with _cache_lock:
            # Re-check inside the lock — another coroutine may have
            # refreshed the cache while we were waiting for the lock.
            if _cache is None or _cache.expires_at <= now:
                try:
                    contexts, entries = await _fetch_catalog()
                    _cache = _CatalogCache(
                        expires_at=now + _TTL_SECONDS,
                        contexts=contexts,
                        entries=entries,
                    )
                except Exception as exc:
                    # Fail-soft: log the error and create a short-lived cache
                    # with empty data. Callers will get the default context
                    # size, and the cache will be retried in 60 seconds.
                    logfire.warning(
                        "model catalog fetch failed, using default {default}",
                        default=default, exc=str(exc),
                    )
                    _cache = _CatalogCache(expires_at=now + 60, contexts={})

    return _cache


async def get_model_context(model_name: str) -> int:
    """Return ``max_model_len`` for ``model_name``, with TTL caching and fallback.

    If the model is not found in the catalog (or the catalog fetch failed),
    returns ``DEFAULT_MODEL_CONTEXT`` from config (default: 200,000 tokens).

    Args:
        model_name: The model identifier (e.g. "anthropic/claude-sonnet-4.6").

    Returns:
        The model's context window size in tokens.
    """
    cache = await _ensure_cache()
    return cache.contexts.get(model_name, get_settings().DEFAULT_MODEL_CONTEXT)


async def list_llm_models(min_context: int = 0) -> list[str]:
    """List model names whose metadata.type is 'llm'.

    Optionally filters to models with at least ``min_context`` tokens.
    Used by the UI to populate the "Cloud model" dropdown with only
    text-generation models (excluding embedding models, etc.).

    Args:
        min_context: Minimum context window size. Models with smaller
                     windows are excluded. Default 0 = no filtering.

    Returns:
        Sorted list of model name strings.
    """
    cache = await _ensure_cache()
    names: list[str] = []
    for entry in cache.entries:
        # Filter to LLM-type models only (skip embedding, reranking, etc.)
        if entry.get("metadata", {}).get("type") != "llm":
            continue
        # Optionally filter by minimum context window size.
        if min_context and entry.get("max_model_len", 0) < min_context:
            continue
        if name := entry.get("id"):
            names.append(name)
    return sorted(names)
