"""CLI entry-point for the embedder service.

This module provides a command-line interface for managing review embeddings.
It is run ad-hoc (not as a long-lived service) via ``docker exec`` or locally:

Usage::

    python -m bankiru.embedder backfill            # embed un-embedded reviews
    python -m bankiru.embedder build-index         # create HNSW index only
    python -m bankiru.embedder reindex             # dry-run: show what would happen
    python -m bankiru.embedder reindex --confirm   # drop all + rebuild from scratch

The embedder is separate from the API service because:
  - Backfill runs automatically at API startup (app.py lifespan), but may
    need to be triggered manually after failures or model changes.
  - Reindex is a destructive operation that should only be run intentionally.

Connection to other modules:
  - bankiru.embedder.__init__ — provides backfill_embeddings() and reindex_embeddings()
  - bankiru.db                — provides get_engine(), get_session_maker(), ensure_hnsw_index()
  - bankiru.logging           — provides Logfire configuration
"""

from __future__ import annotations

import argparse
import asyncio

from bankiru.db import ensure_hnsw_index, get_engine, get_session_maker
from bankiru.embedder import backfill_embeddings, reindex_embeddings
from bankiru.logging import configure_logfire


async def _run_backfill() -> None:
    """Async wrapper for the backfill command.

    Initialises the database engine (required for session creation) and
    runs the backfill process to embed all un-embedded reviews.
    """
    # get_engine() must be called first to initialise the singleton engine
    # and session maker before any database operations.
    get_engine()
    session_maker = get_session_maker()
    await backfill_embeddings(session_maker)


async def _run_build_index(*, force: bool) -> None:
    """Create (or rebuild) the HNSW index without touching embedding rows."""
    get_engine()
    await ensure_hnsw_index(force=force)


async def _run_reindex(confirm: bool) -> None:
    """Async wrapper for the reindex command.

    Without --confirm, this is a dry-run that shows what would happen.
    With --confirm, it drops all embeddings and rebuilds from scratch.
    """
    get_engine()
    session_maker = get_session_maker()
    await reindex_embeddings(session_maker, confirm=confirm)


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command.

    Configures Logfire for service identity and structured logging. Auto-tracing
    is not installed here: ``python -m bankiru.embedder`` loads the package
    ``__init__`` before this module, so Logfire cannot patch it. Explicit
    spans in ``bankiru.embedder`` cover the important paths.
    """
    configure_logfire(service_name="embedder")

    # Build the CLI argument parser with subcommands.
    parser = argparse.ArgumentParser(
        prog="python -m bankiru.embedder",
        description="Manage review embeddings (backfill / build-index / reindex).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # `backfill` — embed reviews that don't have a vector yet.
    sub.add_parser("backfill", help="Embed reviews that don't have a vector yet.")

    # `build-index` — create HNSW index on existing embeddings (recovery path).
    build_index_parser = sub.add_parser(
        "build-index",
        help="Create the HNSW index on review_embeddings (no re-embed).",
    )
    build_index_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Drop and rebuild the index even if it already exists.",
    )

    # `reindex` — destructive: drop all embeddings and rebuild.
    reindex_parser = sub.add_parser(
        "reindex",
        help="Drop all embeddings and rebuild from scratch.",
    )
    reindex_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Actually perform the reindex (without this flag it's a dry-run).",
    )

    args = parser.parse_args()

    # Dispatch to the appropriate async function.
    if args.command == "backfill":
        asyncio.run(_run_backfill())
    elif args.command == "build-index":
        asyncio.run(_run_build_index(force=args.force))
    elif args.command == "reindex":
        asyncio.run(_run_reindex(args.confirm))


if __name__ == "__main__":
    main()
