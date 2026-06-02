"""FastAPI application factory.

This module creates and configures the FastAPI application instance used by
the API service. It is imported by uvicorn at startup (via the import string
``bankiru.api.app:app``).

Responsibilities:
  - Define the application lifespan (startup/shutdown hooks):
      1. Bootstrap the database schema (tables, indexes, pgvector extension)
      2. Spawn a background task to backfill embeddings for any reviews that
         don't yet have a vector (e.g. after initial deployment or if the
         embeddings API was temporarily unavailable during a POST /reviews)
  - Create the FastAPI app with metadata (title, version, contact)
  - Instrument the app with Logfire for distributed tracing
  - Include the API router (all /reviews and /healthz routes)

Connection to other modules:
  - bankiru.api.routes   — provides the router with all HTTP endpoints
  - bankiru.db           — provides create_all_tables() and session factory
  - bankiru.embedder     — provides backfill_embeddings() for the background task
  - bankiru.__init__     — provides __version__ for the OpenAPI spec
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI

from bankiru import __version__
from bankiru.api.routes import router
from bankiru.db import create_all_tables, get_session_maker


async def _backfill_background() -> None:
    """Background task: embed reviews that don't yet have embeddings.

    This runs as an asyncio.Task spawned during the lifespan startup phase.
    It imports backfill_embeddings lazily to avoid circular imports (the
    embedder module imports from bankiru.models which is also used by routes).

    The task is fire-and-forget: if it fails, the API continues to serve
    requests normally. Failed embeddings will be retried on the next restart
    or can be triggered manually via ``python -m bankiru.embedder backfill``.

    On first deploy with ~380K existing reviews, this task processes all of
    them in batches. On subsequent restarts it's typically a no-op because
    all reviews are already embedded.
    """
    try:
        # Lazy import to avoid circular dependency and to ensure the embedder
        # module is only loaded when actually needed.
        from bankiru.embedder import backfill_embeddings
        count = await backfill_embeddings(get_session_maker())
        if count:
            logfire.info("backfill complete: {count} reviews embedded", count=count)
    except asyncio.CancelledError:
        # Graceful shutdown — the lifespan context manager cancels this task
        # when the app is shutting down.
        logfire.info("backfill task cancelled (shutdown)")
    except Exception as exc:
        # Log but don't crash — the API should keep serving even if the
        # embeddings backfill fails. The missing embeddings will be retried
        # on the next restart.
        logfire.warning("backfill task failed: {exc}", exc=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: runs once at startup and once at shutdown.

    Startup:
      1. create_all_tables() — ensures the pgvector extension, ORM tables,
         B-tree indexes, and HNSW vector index all exist (idempotent).
      2. Spawn the embedding backfill as a non-blocking background task.

    Shutdown:
      Cancel the backfill task if it's still running.

    The ``yield`` separates startup from shutdown logic (FastAPI lifespan
    protocol). Everything before ``yield`` runs at startup; everything
    after runs at shutdown.
    """
    # Startup: bootstrap DB schema (tables + indexes)
    await create_all_tables()
    # Spawn embedding backfill as a background task — does not block startup.
    # The task embeds any reviews that don't yet have embeddings.
    # On first deploy this processes all 380K existing reviews; on subsequent
    # restarts it's a no-op (all rows already embedded).
    task = asyncio.create_task(_backfill_background())
    yield
    # Shutdown: cancel the background task if still running
    task.cancel()


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application.

    The app is configured with:
      - lifespan: DB bootstrap + embedding backfill on startup
      - title/version/contact: metadata shown in the Swagger UI (/docs)
      - Logfire instrumentation: all requests except /healthz are traced
        (healthz is excluded to avoid noisy traces from Docker healthchecks)
      - The API router: all /reviews, /healthz, and root redirect routes
    """
    app = FastAPI(
        lifespan=lifespan,
        title="Banki.ru Claims and Negative Reviews Database API",
        version=__version__,
        contact={
            "name": "Evgeny Meredelin",
            "email": "eimeredelin@sberbank.ru",
        },
    )
    # Instrument with Logfire for distributed tracing. Exclude /healthz to
    # avoid generating a trace for every Docker healthcheck poll (every 30s).
    logfire.instrument_fastapi(app, excluded_urls="/healthz")
    # Mount all API routes from the router module.
    app.include_router(router)
    return app


# Module-level app instance: uvicorn imports this directly via the
# "bankiru.api.app:app" import string in __main__.py.
app = create_app()
