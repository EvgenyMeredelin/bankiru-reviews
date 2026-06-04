"""FastAPI dependency helpers.

This module defines reusable FastAPI dependencies that are injected into
route handlers via ``Depends()``. Using type aliases (``DBSession``,
``BotoClient``) keeps route signatures clean and avoids repeating the
``Annotated[..., Depends(...)]`` boilerplate in every handler.

Three dependencies are provided:
  1. DBSession  — an async SQLAlchemy session (one per request, auto-closed)
  2. BotoClient — an async S3 client (one per request, auto-closed)
  3. api_token  — validates the API-Token header for write endpoints

Connection to other modules:
  - bankiru.db              — provides get_async_session (session factory)
  - bankiru.api.botocore_client — provides get_async_client (S3 client factory)
  - bankiru.config          — provides API_TOKEN for authentication
  - bankiru.api.routes      — consumes all three dependencies in route handlers
"""

from __future__ import annotations

from typing import Annotated

from aiobotocore.client import AioBaseClient
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from bankiru.api.botocore_client import get_async_client
from bankiru.config import get_settings
from bankiru.db import get_async_session

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


async def api_token(
    token: Annotated[str, Depends(APIKeyHeader(name="API-Token"))],
) -> None:
    """Validate the API-Token header against the configured secret.

    This dependency is used on write endpoints (POST /reviews, DELETE /reviews,
    etc.) to ensure only authorized clients (the parser service) can modify
    data. Read endpoints (GET /reviews, GET /healthz) do not require this
    token — they are public.

    The APIKeyHeader extractor automatically reads the "API-Token" header
    from the request. If the header is missing, FastAPI returns 422; if the
    token doesn't match, we return 403 Forbidden.

    The parser sends this token in every POST request (see parser/runner.py).
    The token value is set via the API_TOKEN environment variable.
    """
    if token != get_settings().API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
