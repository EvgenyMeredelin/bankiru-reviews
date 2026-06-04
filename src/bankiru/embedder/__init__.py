"""Embedding utilities: generate vectors via Cloud.ru and backfill the DB.

This module provides three main capabilities:

  1. **embed_texts()** — Core function that calls the OpenAI-compatible
     /v1/embeddings endpoint to generate vector embeddings for a list of
     text strings. Used by both the API (for real-time embedding of new
     reviews and semantic search queries) and the backfill process.

  2. **backfill_embeddings()** — Iterates over all reviews that don't yet
     have an embedding and generates one for each. Called automatically
     during API startup (as a background task in app.py) and can also be
     run manually via ``python -m bankiru.embedder backfill``.

  3. **reindex_embeddings()** — Drops all existing embeddings and rebuilds
     them from scratch. Useful when changing the embedding model,
     dimensions, or passage format. Run via
     ``python -m bankiru.embedder reindex --confirm``.

  4. **format_review_for_embedding()** — Builds enriched passage text
     (``bankName | product | location`` + review body) for storage vectors.

The embedding model (BAAI/bge-m3) produces 1024-dimensional vectors that
are stored in the review_embeddings table (pgvector Vector(1024) column).
BGE-M3 query/passage prefixes are applied in ``embed_texts()`` so search
queries and stored passages use asymmetric encoding. These vectors enable
semantic search via cosine similarity in GET /reviews.

Connection to other modules:
  - bankiru.api.routes     — calls embed_texts() for POST /reviews (new review
                             embedding) and GET /reviews (query embedding)
  - bankiru.api.app        — calls backfill_embeddings() as a background task
  - bankiru.embedder.__main__ — CLI entry point for manual backfill/reindex
  - bankiru.db             — provides _progress_bar() for visual feedback
  - bankiru.models         — provides Review and ReviewEmbedding ORM models
  - bankiru.config         — provides embedding API credentials and settings
"""

from __future__ import annotations

import asyncio
import math
import time as _time
from typing import TYPE_CHECKING, Callable, Literal

import httpx
import logfire
from sqlalchemy import func, insert, select, text

from bankiru.config import get_settings
from bankiru.db import _progress_bar, ensure_hnsw_index
from bankiru.models import Review, ReviewEmbedding

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _fmt_duration(seconds: float) -> str:
    """Format *seconds* as ``Xm Ys`` or ``Xs``."""
    m, s = divmod(int(seconds), 60)
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# BGE-M3 retrieval instructions (asymmetric query vs passage encoding).
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_PASSAGE_PREFIX = "Represent this document for retrieval: "


def format_review_for_embedding(
    *,
    bank_name: str,
    product: str,
    location: str,
    review_body: str,
) -> str:
    """Build passage text for embedding: metadata header + review body."""
    parts = [bank_name, product]
    loc = location.strip()
    if loc:
        parts.append(loc)
    return f"{' | '.join(parts)}\n{review_body}"


def _apply_embedding_mode(text: str, mode: Literal["query", "passage"]) -> str:
    prefix = _QUERY_PREFIX if mode == "query" else _PASSAGE_PREFIX
    return prefix + text


# ---------------------------------------------------------------------------
# Core embedding function
# ---------------------------------------------------------------------------

async def embed_texts(
    texts: list[str],
    *,
    mode: Literal["query", "passage"] = "passage",
) -> list[list[float]]:
    """Call the OpenAI-compatible ``/v1/embeddings`` endpoint in batches.

    Splits the input texts into sub-batches of EMBEDDINGS_BATCH_SIZE (default 50)
    and sends each sub-batch as a single API call. This is more efficient than
    one-text-per-call and avoids hitting request size limits.

    Applies BGE-M3 retrieval prefixes via *mode* before calling the API:
      - ``query`` — user search strings (GET /reviews semantic search)
      - ``passage`` — review documents (POST /reviews, backfill, reindex)

    Returns a flat list of embedding vectors in the same order as *texts*.
    Retries on 429 (rate limit) / 5xx (server error) up to 3 times with
    exponential back-off.

    Args:
        texts: List of text strings to embed.
        mode: BGE-M3 encoding mode (query vs passage prefix).

    Returns:
        List of float vectors, one per input text, in the same order.

    Raises:
        RuntimeError: If no API key is configured.
        httpx.HTTPStatusError: If the API returns a non-retryable error.
    """
    settings = get_settings()
    if texts:
        texts = [_apply_embedding_mode(t, mode) for t in texts]

    # Fall back to the general OpenAI key if no embeddings-specific key is set.
    # This allows using a different provider/key for embeddings vs summaries.
    api_key = settings.EMBEDDINGS_API_KEY or settings.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError(
            "No embeddings API key configured. "
            "Set EMBEDDINGS_API_KEY or OPENAI_API_KEY."
        )

    # Build the embeddings endpoint URL from the base URL.
    base_url = settings.EMBEDDINGS_BASE_URL or settings.OPENAI_BASE_URL
    url = base_url.rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    batch_size = settings.EMBEDDINGS_BATCH_SIZE
    all_embeddings: list[list[float]] = []

    # Use a single httpx client for all sub-batches (connection reuse).
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), batch_size):
            sub_batch = texts[start : start + batch_size]
            payload = {"model": settings.EMBEDDINGS_MODEL, "input": sub_batch}

            resp = await _post_with_retry(client, url, headers, payload)
            # The API may return embeddings in arbitrary order; sort by the
            # `index` field to guarantee the output order matches the input.
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            all_embeddings.extend(d["embedding"] for d in data)

    # Sanity-check the API response before writing vectors to pgvector.
    # A count or dimension mismatch would corrupt semantic search or fail at
    # INSERT time with a cryptic Postgres error — fail early with a clear message.
    expected_dim = settings.EMBEDDINGS_DIMENSIONS
    if len(all_embeddings) != len(texts):
        raise RuntimeError(
            f"Embeddings API returned {len(all_embeddings)} vectors for "
            f"{len(texts)} inputs"
        )
    for i, vec in enumerate(all_embeddings):
        if len(vec) != expected_dim:
            raise RuntimeError(
                f"Embedding {i} has dimension {len(vec)}, expected {expected_dim}"
            )

    return all_embeddings

async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict,
    *,
    max_retries: int = 3,
) -> httpx.Response:
    """POST with exponential back-off on 429 (rate limit) / 5xx (server error).

    Retries up to max_retries times with doubling backoff (1s, 2s, 4s).
    On the final attempt, raises the HTTP error if still failing.
    Non-retryable errors (4xx other than 429) are raised immediately.
    """
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        resp = await client.post(url, json=payload, headers=headers)
        # Retry on rate limit (429) or server errors (5xx).
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                # Final attempt — raise the error to the caller.
                resp.raise_for_status()
            logfire.warning(
                "Embeddings API {status} on attempt {attempt}/{max}, "
                "retrying in {backoff:.0f}s",
                status=resp.status_code,
                attempt=attempt,
                max=max_retries,
                backoff=backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp
    # Unreachable, but keeps mypy happy
    raise RuntimeError("Exhausted retries")  # pragma: no cover


# ---------------------------------------------------------------------------
# Backfill un-embedded reviews
# ---------------------------------------------------------------------------

async def backfill_embeddings(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Embed all reviews that don't yet have a vector.

    Iterates in batches of EMBEDDINGS_BACKFILL_BATCH (default 500) rows,
    fetching un-embedded reviews via a LEFT JOIN + IS NULL pattern. Each
    batch makes ceil(batch_size / EMBEDDINGS_BATCH_SIZE) API calls to the
    embeddings endpoint.

    Progress is logged with a visual progress bar and ETA estimate.
    An optional progress_callback can be provided for external monitoring.

    Args:
        session_maker: Async SQLAlchemy session factory.
        progress_callback: Optional (done, total) callback for progress tracking.

    Returns:
        Number of new embeddings created.
    """
    settings = get_settings()
    batch_size = settings.EMBEDDINGS_BACKFILL_BATCH

    # Count how many reviews don't have embeddings yet.
    # LEFT JOIN + IS NULL finds reviews with no corresponding embedding row.
    async with session_maker() as session:
        count_result = await session.execute(
            select(func.count())
            .select_from(Review)
            .outerjoin(ReviewEmbedding, ReviewEmbedding.review_id == Review.id)
            .where(ReviewEmbedding.review_id.is_(None))
        )
        total = count_result.scalar_one()

    if total == 0:
        logfire.info("All reviews already embedded")
        return 0

    total_batches = math.ceil(total / batch_size)
    logfire.info(
        "Backfill starting: {total} un-embedded reviews in ~{batches} batches",
        total=total,
        batches=total_batches,
    )
    embedded = 0
    batch_num = 0
    t0 = _time.monotonic()
    # Review IDs whose embedding batch failed in this run. Excluded from later
    # SELECTs so a broken batch does not get re-fetched on every loop iteration.
    skipped_ids: set[int] = set()

    # Main backfill loop: fetch un-embedded reviews in batches, embed them,
    # and insert the embeddings into the database.
    while True:
        # Fetch the next batch of reviews that don't have embeddings.
        # ORDER BY id ensures deterministic pagination (always processes
        # the oldest un-embedded reviews first).
        async with session_maker() as session:
            stmt = (
                select(
                    Review.id,
                    Review.bankName,
                    Review.product,
                    Review.location,
                    Review.reviewBody,
                )
                .outerjoin(
                    ReviewEmbedding, ReviewEmbedding.review_id == Review.id,
                )
                .where(ReviewEmbedding.review_id.is_(None))
                .order_by(Review.id)
                .limit(batch_size)
            )
            # Exclude batches that already failed in this run (see except below).
            if skipped_ids:
                stmt = stmt.where(Review.id.not_in(skipped_ids))
            result = await session.execute(stmt)
            rows = result.all()

        if not rows:
            # No more un-embedded reviews — backfill is complete.
            break

        batch_num += 1
        ids = [r.id for r in rows]
        texts = [
            format_review_for_embedding(
                bank_name=r.bankName,
                product=r.product,
                location=r.location,
                review_body=r.reviewBody,
            )
            for r in rows
        ]

        try:
            # Generate embeddings for this batch of review texts.
            # embed_texts() handles sub-batching internally.
            vectors = await embed_texts(texts, mode="passage")
        except Exception:
            # Skip this batch for the rest of this run so a persistent failure
            # does not spin forever. Rows stay un-embedded until the next backfill.
            skipped_ids.update(ids)
            logfire.warning(
                "Batch {batch}/{batches} starting at review_id={first_id} failed, "
                "skipping {n} reviews for this run",
                batch=batch_num,
                batches=total_batches,
                first_id=ids[0],
                n=len(ids),
            )
            await asyncio.sleep(2.0)
            continue

        # Bulk insert the generated embeddings into the database.
        async with session_maker() as session:
            await session.execute(
                insert(ReviewEmbedding).values(
                    [
                        {"review_id": rid, "embedding": vec}
                        for rid, vec in zip(ids, vectors)
                    ]
                )
            )
            await session.commit()

        # Update progress tracking and log with visual progress bar.
        embedded += len(ids)
        elapsed = _time.monotonic() - t0
        rate = embedded / elapsed if elapsed > 0 else 0
        eta = (total - embedded) / rate if rate > 0 else 0
        logfire.info(
            "{bar}  batch {batch}/{batches}  {done}/{total}  "
            "elapsed {elapsed}  ETA {eta}",
            bar=_progress_bar(embedded, total),
            batch=batch_num,
            batches=total_batches,
            done=embedded,
            total=total,
            elapsed=_fmt_duration(elapsed),
            eta=_fmt_duration(eta),
        )
        if progress_callback is not None:
            progress_callback(embedded, total)

        # Brief pause between batches to respect API rate limits.
        await asyncio.sleep(0.5)

    total_elapsed = _time.monotonic() - t0
    logfire.info(
        "Backfill complete: {embedded} new embeddings in {elapsed}",
        embedded=embedded,
        elapsed=_fmt_duration(total_elapsed),
    )
    return embedded


# ---------------------------------------------------------------------------
# Full reindex (drop + rebuild)
# ---------------------------------------------------------------------------

async def reindex_embeddings(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    confirm: bool = False,
) -> None:
    """Drop all embeddings and rebuild from scratch.

    This is a destructive operation that:
      1. TRUNCATEs the review_embeddings table (fast, no per-row overhead)
      2. Drops the HNSW vector index (must be rebuilt after bulk insert)
      3. Runs backfill_embeddings() to re-embed all reviews
      4. Rebuilds the HNSW index with increased maintenance_work_mem

    Without ``confirm=True`` this is a **dry-run** that only prints what
    *would* happen. This safety mechanism prevents accidental data loss.

    Use cases:
      - Changing the embedding model (e.g. from bge-m3 to a newer model)
      - Changing the vector dimensions
      - Changing the passage format (enriched metadata + BGE-M3 prefixes)
      - Recovering from corrupted embeddings
    """
    # ── Dry-run: show what would happen without making changes ────────
    if not confirm:
        async with session_maker() as session:
            total = (
                await session.execute(select(func.count()).select_from(Review))
            ).scalar_one()
            existing = (
                await session.execute(
                    select(func.count()).select_from(ReviewEmbedding)
                )
            ).scalar_one()

        logfire.info(
            "Would reindex {total} reviews. "
            "{existing} existing embeddings will be deleted. "
            "Run with --confirm to proceed.",
            total=total,
            existing=existing,
        )
        return

    # ── Full reindex ──────────────────────────────────────────────────
    t0 = _time.monotonic()

    # Step 1: TRUNCATE is much faster than DELETE for removing all rows
    # (no per-row WAL logging, no trigger firing).
    # Step 2: Drop the HNSW index before bulk insert — building the index
    # after all data is inserted is much faster than maintaining it during
    # incremental inserts.
    async with session_maker() as session:
        await session.execute(text("TRUNCATE bankiru.review_embeddings"))
        await session.execute(
            text("DROP INDEX IF EXISTS bankiru.ix_review_embeddings_hnsw")
        )
        await session.commit()
    logfire.info("Truncated review_embeddings and dropped HNSW index")

    # Step 3: Re-embed all reviews using the standard backfill function.
    await backfill_embeddings(session_maker)

    # Step 4: Rebuild the HNSW index (shared helper; no re-embed).
    await ensure_hnsw_index(force=True)

    elapsed = _time.monotonic() - t0
    logfire.info("Reindex complete in {elapsed:.1f}s", elapsed=elapsed)
