"""FastAPI dependency helpers.

This module defines reusable FastAPI dependencies that are injected into
route handlers via ``Depends()``. Using type aliases (``DBSession``,
``BotoClient``) keeps route signatures clean and avoids repeating the
``Annotated[..., Depends(...)]`` boilerplate in every handler.

Dependencies:
  1. DBSession  — an async SQLAlchemy session (one per request, auto-closed)
  2. BotoClient — an async S3 client (one per request, auto-closed)
  3. api_token  — privileged API-Token for write endpoints (API_TOKEN only)
  4. guest_or_admin_token_if_gateway — on public Nginx gateway requests,
     require API-Token ∈ GUEST_API_TOKEN or API_TOKEN; internal callers skip

Connection to other modules:
  - bankiru.db              — provides get_async_session (session factory)
  - bankiru.api.botocore_client — provides get_async_client (S3 client factory)
  - bankiru.config          — provides API_TOKEN / GUEST_API_TOKEN
  - bankiru.api.routes      — consumes these dependencies in route handlers
"""

from __future__ import annotations

import hmac
from typing import Annotated

from aiobotocore.client import AioBaseClient
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from bankiru.api.botocore_client import get_async_client
from bankiru.config import get_settings
from bankiru.db import get_async_session

# Header set by Nginx when proxying https://bankiru.uva-advanced.ru → api.
# Must be overwritten by the proxy (not taken from the client). Internal
# callers omit this header and skip the GET /reviews token check.
GATEWAY_HEADER = "X-Bankiru-Gateway"
GATEWAY_HEADER_VALUE = "1"

# ── Type aliases for FastAPI dependency injection ────────────────────────
# These allow route handlers to declare dependencies with a single type
# annotation instead of the verbose Annotated[..., Depends(...)] form.
#
# Example usage in a route handler:
#   async def get_reviews(session: DBSession, client: BotoClient): ...

# Async SQLAlchemy session — yields one session per request, auto-closed
# when the request handler finishes (via the async generator protocol in
# get_async_session).
DBSession = Annotated[AsyncSession, Depends(get_async_session)]

# Async S3 client — yields one client per request, auto-closed when the
# request handler finishes (via the async generator in get_async_client).
BotoClient = Annotated[AioBaseClient, Depends(get_async_client)]

# Privileged write auth: a missing or empty header is rejected by APIKeyHeader
# itself with 401, before api_token runs; a present but wrong token gives 403.
_api_token_header = APIKeyHeader(name="API-Token")

# Gateway GET auth: missing header must not reject internal UI calls.
_optional_api_token_header = APIKeyHeader(name="API-Token", auto_error=False)


def _token_matches(candidate: str, expected: str) -> bool:
    """Constant-time equality; False if lengths differ (compare_digest raises)."""
    if len(candidate) != len(expected):
        return False
    return hmac.compare_digest(candidate, expected)


async def api_token(
    token: Annotated[str, Depends(_api_token_header)],
) -> None:
    """Validate the API-Token header against the privileged ``API_TOKEN``.

    Used on write endpoints (POST /reviews, DELETE /reviews, etc.). Guest
    tokens from ``GUEST_API_TOKEN`` are never accepted here.

    Missing or empty header → 401 (raised by APIKeyHeader before this runs).
    Present but wrong value → 403.
    """
    if not _token_matches(token, get_settings().API_TOKEN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


async def guest_or_admin_token_if_gateway(
    request: Request,
    token: Annotated[str | None, Depends(_optional_api_token_header)],
) -> None:
    """Require a guest or admin token only for public-gateway GET requests.

    When Nginx proxies ``https://bankiru.uva-advanced.ru/reviews``, it sets
    ``X-Bankiru-Gateway: 1``. In that case the client must send ``API-Token``
    equal to ``API_TOKEN`` or one of ``GUEST_API_TOKEN``.

    Internal callers (Gradio UI → ``http://api:1706``, parser on other
    methods) do not send the gateway header and skip this check. Write
    routes still use :func:`api_token` and accept only ``API_TOKEN``.
    """
    if request.headers.get(GATEWAY_HEADER) != GATEWAY_HEADER_VALUE:
        return

    if not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    settings = get_settings()
    if _token_matches(token, settings.API_TOKEN):
        return
    for guest in settings.guest_api_tokens:
        if _token_matches(token, guest):
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
