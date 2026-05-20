"""Lazy, fail-soft, TTL-cached LLM list for the UI dropdown."""

from __future__ import annotations

import asyncio
import time

import logfire

from bankiru.api.model_catalog import list_llm_models
from bankiru.config import get_settings

_TTL_SECONDS = 60 * 60
_FALLBACK = ["anthropic/claude-sonnet-4.6", "google/gemini-3.1-pro-preview"]

_cache_value: list[str] | None = None
_cache_expires_at: float = 0.0


async def _refresh() -> list[str]:
    try:
        models = await list_llm_models()
        return models or _FALLBACK
    except Exception as exc:
        logfire.warning("foundation-models list failed: {exc}", exc=str(exc))
        return _FALLBACK


def list_foundation_models() -> list[str]:
    """Synchronous wrapper for Gradio. Returns the cached list or fetches it.

    On import-time Gradio invocation (no running loop) it uses `asyncio.run`;
    inside a running loop it dispatches via `run_until_complete` on a side
    loop to keep the call non-blocking-friendly. Always returns a non-empty
    list, even when Cloud.ru is unreachable.
    """
    global _cache_value, _cache_expires_at

    now = time.monotonic()
    if _cache_value is not None and _cache_expires_at > now:
        return _cache_value

    if not get_settings().OPENAI_API_KEY:
        _cache_value = _FALLBACK
        _cache_expires_at = now + _TTL_SECONDS
        return _cache_value

    try:
        result = asyncio.run(_refresh())
    except RuntimeError:
        # We're inside a running loop: schedule on a fresh loop.
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_refresh())
        finally:
            loop.close()

    _cache_value = result
    _cache_expires_at = now + _TTL_SECONDS
    return _cache_value
