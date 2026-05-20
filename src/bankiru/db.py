"""Async SQLAlchemy engine, session factory, and table bootstrap."""

from __future__ import annotations

import time as _time
from collections.abc import AsyncGenerator

import logfire
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateIndex

from bankiru.config import get_settings
from bankiru.models import Base, Review

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
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
    if _session_maker is None:
        get_engine()
    if _session_maker is None:
        raise RuntimeError("Session maker was not initialised by get_engine()")
    return _session_maker


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_maker()() as session:
        yield session


def _progress_bar(done: int, total: int, *, width: int = 20) -> str:
    """Return a text progress bar, e.g. ``[########............]  40%``."""
    frac = done / total if total else 1.0
    filled = int(width * frac)
    return "[" + "#" * filled + "." * (width - filled) + "]" + f"  {frac:>4.0%}"


async def create_all_tables() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # `create_all` skips tables that already exist, so indexes added to
    # an existing model are silently ignored.  Ensure every model-defined
    # index exists by issuing CREATE INDEX IF NOT EXISTS for each one.
    # Disable statement_timeout for this block — index creation on a
    # large existing table can exceed the default 300 s limit.
    indexes = list(Review.__table__.indexes)
    total = len(indexes)
    t0 = _time.monotonic()
    async with get_engine().begin() as conn:
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
    logfire.info("all indexes ensured in {elapsed:.1f} s", elapsed=total_elapsed)
