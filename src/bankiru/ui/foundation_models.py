"""Lazy, fail-soft, TTL-cached LLM list for the UI dropdown.

This module provides a synchronous function (list_foundation_models) that
returns the list of available LLM models for the "Summary model" dropdown in
the Gradio UI. It wraps the async model_catalog.list_llm_models() function
with a TTL cache and fail-soft fallback.

Design challenges:
  - Gradio calls dropdown choice functions synchronously during Blocks
    construction (at import time), but the model catalog API is async.
  - The Cloud.ru API may be unreachable, so we need a fallback list.
  - Multiple requests shouldn't hammer the API, so we cache for 1 hour.

Solution:
  - TTL cache with module-level globals (simple, no threading concerns)
  - asyncio.run() for the initial call (no running loop at import time)
  - Fallback to asyncio.new_event_loop() if called inside a running loop
  - Hardcoded _FALLBACK list ensures the dropdown is never empty

Connection to other modules:
  - bankiru.api.model_catalog — provides list_llm_models() (async, TTL-cached)
  - bankiru.ui.blocks         — calls list_foundation_models() to populate
                                the "Summary model" dropdown
  - bankiru.config            — provides OPENAI_API_KEY (needed for API access)
"""

from __future__ import annotations

import asyncio
import time

import logfire

from bankiru.api.model_catalog import list_llm_models
from bankiru.config import get_settings

# Cache TTL: 1 hour. Matches the TTL in model_catalog.py.
_TTL_SECONDS = 60 * 60

# Fallback model list used when the Cloud.ru API is unreachable or no API
# key is configured. These are popular models that are likely to be available.
_FALLBACK = ["anthropic/claude-sonnet-4.6", "google/gemini-3.1-pro-preview"]

# Module-level cache: stores the most recent model list and its expiry time.
_cache_value: list[str] | None = None
_cache_expires_at: float = 0.0


async def _refresh() -> list[str]:
    """Fetch the model list from the catalog API, falling back on error.

    Returns the fetched list if successful, or _FALLBACK if the API call
    fails or returns an empty list.
    """
    try:
        models = await list_llm_models()
        return models or _FALLBACK
    except Exception as exc:
        logfire.warning("foundation-models list failed: {exc}", exc=str(exc))
        return _FALLBACK


def list_foundation_models() -> list[str]:
    """Synchronous wrapper for Gradio. Returns the cached list or fetches it.

    This function handles the sync/async boundary that Gradio requires:
      - At import time (no running event loop): uses asyncio.run()
      - Inside a running loop (e.g. during a Gradio event handler):
        creates a temporary event loop to avoid "cannot run nested" errors

    Always returns a non-empty list, even when Cloud.ru is unreachable
    (falls back to _FALLBACK).

    Returns:
        Sorted list of available LLM model names.
    """
    global _cache_value, _cache_expires_at

    now = time.monotonic()
    # Fast path: return cached value if still valid.
    if _cache_value is not None and _cache_expires_at > now:
        return _cache_value

    # If no API key is configured, skip the network call entirely
    # and use the hardcoded fallback list.
    if not get_settings().OPENAI_API_KEY:
        _cache_value = _FALLBACK
        _cache_expires_at = now + _TTL_SECONDS
        return _cache_value

    # Attempt to fetch the model list from the async API.
    try:
        # asyncio.run() works when there's no running event loop
        # (e.g. during module import / Blocks construction).
        result = asyncio.run(_refresh())
    except RuntimeError:
        # RuntimeError: "cannot be called from a running event loop"
        # This happens when Gradio calls us from inside an async context.
        # Create a temporary event loop to run the async function.
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_refresh())
        finally:
            loop.close()

    # Update the cache with the fetched result.
    _cache_value = result
    _cache_expires_at = now + _TTL_SECONDS
    return _cache_value
