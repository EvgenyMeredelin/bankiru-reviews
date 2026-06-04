"""Async SQLAlchemy engine, session factory, and table bootstrap.

This module is the single point of database access for the entire project.
All services (api, parser via the api, embedder, ui via the api) ultimately
go through the engine and session factory created here.

Key responsibilities:
  1. Create a singleton async SQLAlchemy engine connected to Postgres.
  2. Provide an async session factory for use in FastAPI dependency injection
     and in the embedder's batch processing loops.
  3. Bootstrap the database schema on first startup: create the pgvector
     extension, all ORM-defined tables, and ensure every index exists
     (including the HNSW vector index for semantic search).
"""

from __future__ import annotations

import time as _time  # aliased to avoid shadowing with local variables
from collections.abc import AsyncGenerator

import logfire
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
# CreateIndex is used to programmatically emit CREATE INDEX IF NOT EXISTS
# statements for each model-defined index (see create_all_tables step 3).
from sqlalchemy.schema import CreateIndex

from bankiru.config import get_settings
# Import both ORM models so their tables are registered in Base.metadata
# before create_all() is called. ReviewEmbedding is imported even though
# it's not directly referenced in this file — its table definition must
# be present in the metadata for create_all() to create it.
from bankiru.models import Base, Review, ReviewEmbedding  # noqa: F401

# ── Module-level singletons ──────────────────────────────────────────────
# The engine and session maker are lazily initialised on first call to
# get_engine(). Using module-level globals (rather than a class or DI
# container) keeps the API simple — every caller just imports get_engine()
# or get_session_maker(). The trade-off is that these are true process-wide
# singletons, which is fine because each Docker container runs exactly one
# service process.
_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call.

    The engine is configured with:
      - connect_timeout=10: fail fast if the DB host is unreachable, rather
        than hanging the container's healthcheck indefinitely.
      - statement_timeout=300s: a safety net so no single query can block
        the event loop forever. 300 s is generous enough for the heaviest
        operation (full-table SELECT for Parquet backup of ~380K rows) while
        still catching genuinely runaway queries. Individual routes can
        override this per-transaction with SET LOCAL.
      - pool_pre_ping=True: test connections before handing them out, which
        transparently recovers from Postgres restarts or network blips
        without surfacing stale-connection errors to callers.
      - expire_on_commit=False on the session maker: prevents SQLAlchemy
        from lazily re-fetching attributes after commit, which would fail
        in an async context (no implicit IO). Callers get the committed
        state immediately without extra queries.
    """
    global _engine, _session_maker
    if _engine is None:
        # `connect_timeout=10` ensures a stuck network path surfaces as a
        # quick error in the lifespan instead of hanging the healthcheck.
        # `options` sets a default per-statement timeout so no single query
        # can hang the API indefinitely (individual routes may override via
        # SET LOCAL).  300 s is generous enough for full-table SELECTs
        # (the backup path reads every row) while still catching runaway
        # queries that would otherwise hang forever.
        _engine = create_async_engine(
            get_settings().POSTGRES_URL,
            connect_args={
                "connect_timeout": 10,
                "options": "-c statement_timeout=300s",
            },
            pool_pre_ping=True,
        )
        _session_maker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return the singleton session factory.

    Calls get_engine() to ensure the engine is initialised first.
    Used by FastAPI's dependency injection (get_async_session) and by the
    embedder's backfill/reindex functions which manage their own sessions.
    """
    if _session_maker is None:
        get_engine()
    if _session_maker is None:
        raise RuntimeError("Session maker was not initialised by get_engine()")
    return _session_maker


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a single async session per request.

    The session is automatically closed when the request handler finishes
    (via the async generator protocol). This is wired into FastAPI routes
    with ``Depends(get_async_session)``.
    """
    async with get_session_maker()() as session:
        yield session


def _progress_bar(done: int, total: int, *, width: int = 20) -> str:
    """Return a text progress bar, e.g. ``[########............]  40%``.

    Used during index creation and embedding backfill to give operators
    visual feedback in the Logfire / console logs.
    """
    frac = done / total if total else 1.0
    filled = int(width * frac)
    return "[" + "#" * filled + "." * (width - filled) + "]" + f"  {frac:>4.0%}"


_HNSW_INDEX_DDL = (
    "CREATE INDEX ix_review_embeddings_hnsw "
    "ON bankiru.review_embeddings "
    "USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 200)"
)

# pg_indexes alone is not enough: a failed build can leave an invalid index
# that IF NOT EXISTS treats as "already there" and skips rebuilding.
_HNSW_INDEX_STATUS_SQL = text(
    "SELECT ix.indisvalid "
    "FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_index ix ON ix.indexrelid = c.oid "
    "JOIN pg_class t ON t.oid = ix.indrelid "
    "WHERE n.nspname = 'bankiru' "
    "AND t.relname = 'review_embeddings' "
    "AND c.relname = 'ix_review_embeddings_hnsw'"
)


async def ensure_hnsw_index(*, force: bool = False) -> None:
    """Create the HNSW vector index on ``review_embeddings`` if it is missing.

    Uses ``statement_timeout = 0`` and ``maintenance_work_mem = 2GB`` because
    building HNSW on ~380K rows routinely exceeds the engine default (300 s)
    and benefits from extra RAM during construction.

    Drops and rebuilds when the index is missing, invalid, or ``force=True``.
    ``CREATE INDEX IF NOT EXISTS`` is intentionally avoided — it silently skips
    invalid indexes left over from a cancelled build.

    Args:
        force: When True, drop a valid index and rebuild from scratch.
    """
    async with get_engine().begin() as conn:
        row = (await conn.execute(_HNSW_INDEX_STATUS_SQL)).first()

        if row is not None:
            is_valid = bool(row[0])
            if is_valid and not force:
                logfire.info("HNSW index already exists and is valid")
                return
            logfire.info(
                "Dropping HNSW index (force={force}, valid={valid}) …",
                force=force,
                valid=is_valid,
            )
            await conn.execute(
                text("DROP INDEX IF EXISTS bankiru.ix_review_embeddings_hnsw")
            )

        await conn.execute(text("SET LOCAL statement_timeout = 0"))
        await conn.execute(text("SET LOCAL maintenance_work_mem = '2GB'"))
        logfire.info(
            "Building HNSW index on review_embeddings "
            "(may take 15–45+ minutes for ~380K rows) …"
        )
        t0 = _time.monotonic()
        await conn.execute(text(_HNSW_INDEX_DDL))
        elapsed = _time.monotonic() - t0

    async with get_engine().begin() as conn:
        row = (await conn.execute(_HNSW_INDEX_STATUS_SQL)).first()
        if row is None or not row[0]:
            raise RuntimeError(
                "HNSW index ix_review_embeddings_hnsw is missing or invalid "
                "after CREATE INDEX"
            )

    logfire.info("HNSW index ready ({elapsed:.1f} s)", elapsed=elapsed)


async def create_all_tables() -> None:
    """Bootstrap the database schema — called once during API startup (lifespan).

    This function is idempotent: it uses IF NOT EXISTS for the extension,
    tables, and B-tree indexes; the HNSW index is ensured separately via
    ``ensure_hnsw_index()``. Repeated calls on an already-initialised
    database are safe and fast.

    Steps:
      1. Enable the pgvector extension (required before any Vector columns
         can be used).
      2. Create all ORM-defined tables via Base.metadata.create_all().
         This handles the "bankiru" schema creation implicitly.
      3. Explicitly ensure every model-defined B-tree index exists.
         This is necessary because create_all() skips tables that already
         exist, so indexes added to an existing model after initial
         deployment would be silently ignored without this step.
      4. Ensure the HNSW vector index via ``ensure_hnsw_index()`` (validates
         ``indisvalid``, disables statement timeout, sets maintenance_work_mem).
    """
    # Step 1 + 2: pgvector extension and ORM tables
    async with get_engine().begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    # Step 3: Ensure all B-tree indexes on the Review table exist.
    # `create_all` skips tables that already exist, so indexes added to
    # an existing model are silently ignored.  Ensure every model-defined
    # index exists by issuing CREATE INDEX IF NOT EXISTS for each one.
    # Disable statement_timeout for this block — index creation on a
    # large existing table can exceed the default 300 s limit.
    indexes = list(Review.__table__.indexes)
    total = len(indexes)
    t0 = _time.monotonic()
    async with get_engine().begin() as conn:
        # Disable statement_timeout: building indexes on a large table
        # (e.g. 380K reviews) can take longer than the default 300 s.
        # SET LOCAL scopes the override to this transaction only.
        await conn.execute(text("SET LOCAL statement_timeout = 0"))
        for i, idx in enumerate(indexes, 1):
            logfire.info(
                "{bar}  [{i}/{total}] creating {name} …",
                bar=_progress_bar(i - 1, total), i=i, total=total,
                name=idx.name,
            )
            idx_start = _time.monotonic()
            await conn.execute(CreateIndex(idx, if_not_exists=True))
            elapsed = _time.monotonic() - idx_start
            logfire.info(
                "{bar}  [{i}/{total}] {name} ready ({elapsed:.1f} s)",
                bar=_progress_bar(i, total), i=i, total=total,
                name=idx.name, elapsed=elapsed,
            )
    total_elapsed = _time.monotonic() - t0
    logfire.info("all B-tree indexes ensured in {elapsed:.1f} s", elapsed=total_elapsed)

    # ── Step 4: HNSW vector index (pgvector) ─────────────────────────────
    await ensure_hnsw_index()
