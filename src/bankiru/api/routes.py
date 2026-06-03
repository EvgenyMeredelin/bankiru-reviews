"""HTTP routes for /reviews and root redirect.

This module defines all HTTP endpoints for the API service:

  Public (no auth):
    GET  /healthz              — Docker healthcheck (excluded from Logfire tracing)
    GET  /                     — redirect to /docs (Swagger UI)
    GET  /reviews              — query reviews with filters, export to S3, summarize

  Protected (API-Token header required):
    POST   /reviews            — bulk insert reviews (called by the parser)
    DELETE /reviews            — delete reviews by ID list
    DELETE /reviews/by-date    — delete reviews within a date range
    DELETE /reviews/duplicates — deduplicate reviews by md5(reviewBody) + product

The GET /reviews endpoint is the most complex: it builds a dynamic SQLAlchemy
query from the filter parameters, optionally performs semantic search via
pgvector, exports the results to S3 in the requested format, generates a
pre-signed download URL, and runs the LLM summarization pipeline.

Connection to other modules:
  - bankiru.api.schemas    — Pydantic models for request/response validation
  - bankiru.api.deps       — FastAPI dependencies (DBSession, BotoClient, api_token)
  - bankiru.api.handlers   — format-specific export handlers (CSV, JSON, Parquet, XLSX)
  - bankiru.api.summarizer — LLM map-reduce summarization (called via handlers)
  - bankiru.embedder       — embed_texts() for semantic search query embedding
  - bankiru.models         — Review and ReviewEmbedding ORM models
  - bankiru.config         — settings for S3 backup prefix, default model, etc.
"""

from __future__ import annotations

import asyncio
import io
from datetime import date, datetime, time
from typing import Annotated

import logfire
import pandas as pd
from aiobotocore.client import AioBaseClient
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, func, insert, or_, select, text
from starlette.responses import RedirectResponse

from bankiru.api import schemas
from bankiru.api.deps import BotoClient, DBSession, api_token
from bankiru.api.schemas import Request, Response, available_output_formats
from bankiru.config import get_settings
from bankiru.embedder import embed_texts
from bankiru.models import Review, ReviewEmbedding

# All routes are registered on this router, which is included in the
# FastAPI app by app.py's create_app() function.
router = APIRouter()


# ── Health check ─────────────────────────────────────────────────────────────
# Lightweight endpoint polled by Docker every 30s (see docker-compose.yml).
# include_in_schema=False hides it from the Swagger UI.
# @logfire.no_auto_trace prevents generating a trace for every poll,
# which would create noisy, low-value spans in the Logfire dashboard.
@router.get("/healthz", include_in_schema=False)
@logfire.no_auto_trace
async def healthz() -> dict[str, str]:
    """Return a simple OK response for Docker healthchecks."""
    return {"status": "ok"}


# ── Root redirect ────────────────────────────────────────────────────────────
# Convenience redirect: visiting the API root shows the Swagger UI.
@router.get("/", include_in_schema=False)
async def redirect_from_root_to_docs():
    """Redirect GET / to the interactive API documentation at /docs."""
    return RedirectResponse(url="/docs")


async def _backup_daily_batch(
    rows: list[dict],
    client: AioBaseClient,
) -> None:
    """Serialize the POSTed batch to Parquet and upload to S3 as a daily backup.

    This creates a date-stamped Parquet file in the S3 backup prefix, e.g.:
      bankiru-reviews/bankiru-reviews-2025-06-01.parquet

    The backup serves as an immutable audit trail of what the parser collected
    each day. If the same date is crawled twice, the second upload overwrites
    the first (S3 PUT is idempotent by key).

    *rows* is the same ``list[dict]`` already prepared for the bulk INSERT,
    so no extra ``model_dump()`` call is needed.  Parquet serialization is
    CPU-bound and is offloaded to a thread to keep ``/healthz`` responsive.

    Args:
        rows: List of review dicts (already validated by Pydantic).
        client: Async S3 client for uploading the backup file.
    """
    settings = get_settings()

    def _serialize() -> tuple[io.BytesIO, str]:
        """CPU-bound: convert rows to Parquet bytes. Runs in a thread."""
        df = pd.DataFrame.from_records(rows)
        # datePublished is already a datetime (validated by Pydantic);
        # extract the date and pick the most common one for the filename.
        # This ensures the backup filename reflects the actual review dates,
        # not the date the crawl ran.
        dates = pd.to_datetime(df["datePublished"]).dt.date
        backup_date = dates.mode().iloc[0] if not dates.empty else date.today()
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        return buf, backup_date.isoformat()

    # Offload serialization to a thread so the event loop stays responsive
    # (important for /healthz during large batch processing).
    buf, date_str = await asyncio.to_thread(_serialize)

    # Upload to S3 with a deterministic key (date-based). Overwrites any
    # existing backup for the same date (idempotent).
    key = f"{settings.OBS_BACKUP_PREFIX}/bankiru-reviews-{date_str}.parquet"
    await client.put_object(
        Bucket=settings.OBS_BUCKET,
        Key=key,
        Body=buf,
        ContentType="application/vnd.apache.parquet",
    )


# ── POST /reviews ────────────────────────────────────────────────────────────
# Called by the parser after each daily crawl to insert new reviews.
# Protected by the api_token dependency (requires API-Token header).
@router.post(
    "/reviews",
    dependencies=[Depends(api_token)],
    status_code=status.HTTP_201_CREATED,
)
async def post_reviews(
    reviews: list[schemas.Review],
    session: DBSession,
    client: BotoClient,
):
    """Insert a batch of reviews, generate embeddings, and create a daily backup.

    Processing steps:
      1. Validate reviews via Pydantic (automatic via FastAPI)
      2. Bulk INSERT all reviews in a single SQL statement
      3. Generate vector embeddings for the new reviews (for semantic search)
      4. Upload a Parquet backup of the batch to S3

    If embedding generation fails, the reviews are still inserted — missing
    embeddings will be backfilled on the next API restart (see app.py lifespan).
    """
    # Fast path: an empty JSON array is valid but there is nothing to insert,
    # embed, or back up. Return 201 with no body instead of running empty SQL.
    if not reviews:
        return None

    with logfire.span("Create new entries"):
        # Validate via Pydantic, then pass raw dicts straight to a bulk
        # INSERT instead of instantiating N ORM objects + N individual
        # INSERT statements.  This emits a single
        #   INSERT INTO … VALUES (…), (…), …
        # round-trip regardless of batch size.
        rows = [r.model_dump() for r in reviews]

    with logfire.span("Bulk insert and commit"):
        await session.execute(insert(Review), rows)
        await session.commit()

    with logfire.span("Embed new reviews"):
        try:
            # We need the IDs of the just-inserted rows. Since we used bulk
            # insert without RETURNING, query them back by matching the URLs
            # (which are unique per review page).
            # LEFT JOIN + IS NULL excludes reviews that already have
            # embeddings (e.g. duplicate URLs that existed before this batch).
            urls = [r.url for r in reviews]
            id_stmt = (
                select(Review.id, Review.reviewBody)
                .outerjoin(ReviewEmbedding, ReviewEmbedding.review_id == Review.id)
                .where(Review.url.in_(urls), ReviewEmbedding.review_id.is_(None))
            )
            id_result = await session.execute(id_stmt)
            id_rows = id_result.all()

            if id_rows:
                review_ids = [row.id for row in id_rows]
                texts = [row.reviewBody for row in id_rows]
                vectors = await embed_texts(texts)

                embedding_rows = [
                    {"review_id": rid, "embedding": vec}
                    for rid, vec in zip(review_ids, vectors)
                ]
                await session.execute(insert(ReviewEmbedding), embedding_rows)
                await session.commit()
        except Exception as exc:
            logfire.warning(
                "embedding failed for POST batch, will be backfilled: {exc}",
                exc=str(exc),
            )

    with logfire.span("Backup daily batch to S3"):
        await _backup_daily_batch(rows, client)


# ── GET /reviews ─────────────────────────────────────────────────────────────
# The main query endpoint. Called by the UI to fetch filtered reviews.
# No authentication required — read access is public.
@router.get("/reviews")
async def get_reviews(
    r: Annotated[Request, Query()],
    session: DBSession,
    client: BotoClient,
):
    """Query reviews with filters, export to S3, and summarize via LLM.

    Query parameters (all optional):
      - startDate/endDate: date range filter (B-tree index on datePublished)
      - bankName: list of bank names (exact match via IN clause)
      - product: list of product labels (exact match via IN clause)
      - location: list of city prefixes (startswith match)
      - keywords: free-text semantic search (pgvector cosine similarity)
      - outputFormat: export format (csv/json/parquet/xlsx, default: parquet)
      - cloudModel: LLM model for summarization (default from config)

    Returns a Response with:
      - filename: S3 object key of the exported file
      - url: pre-signed download URL (valid for 1 hour)
      - comment: LLM-generated summary of the reviews
    """
    with logfire.span("Select entries"):
        # Build a list of WHERE clauses from the query parameters.
        # Only non-None parameters contribute a clause, so an empty query
        # returns all reviews (no WHERE conditions).
        clauses = []

        # ── Date range filter ────────────────────────────────────────
        # Compare datePublished (a DateTime column) directly against
        # datetime boundaries so the B-tree index is usable.  The
        # previous `cast(datePublished, Date)` wrapped the column in a
        # function call, forcing a sequential scan.
        # time.min = 00:00:00, time.max = 23:59:59.999999
        if r.startDate:
            clauses.append(Review.datePublished >= datetime.combine(r.startDate, time.min))
        if r.endDate:
            clauses.append(Review.datePublished <= datetime.combine(r.endDate, time.max))
        # ── Location filter (prefix match) ───────────────────────────
        # Uses startswith() for prefix matching (e.g. "Москва" matches
        # "Москва, район Хамовники"). OR across multiple locations.
        if r.location:
            clauses.append(or_(*[Review.location.startswith(loc) for loc in r.location]))
        # ── Bank name filter (exact match) ───────────────────────────
        if r.bankName:
            clauses.append(Review.bankName.in_(r.bankName))
        # ── Product filter (exact match) ─────────────────────────────
        if r.product:
            clauses.append(Review.product.in_(r.product))

        # ── Semantic search path (keywords provided) ─────────────────
        # When the user provides keywords, we embed the query text and
        # find the most similar reviews using pgvector cosine distance.
        # The result is ordered by similarity (closest first) and capped
        # at SEMANTIC_SEARCH_LIMIT reviews.
        if r.keywords and r.keywords.strip():
            with logfire.span("Embed keywords"):
                try:
                    # Embed the user's query text into a vector using the
                    # same model that was used to embed the review texts.
                    query_vectors = await embed_texts([r.keywords.strip()])
                except Exception as exc:
                    logfire.warning(
                        "Failed to embed keywords: {exc}", exc=str(exc),
                    )
                    # Fail gracefully: return an error message instead of
                    # crashing. The user can retry without keywords.
                    return Response(
                        **r.model_dump(),
                        comment=f"Semantic search unavailable: {exc}",
                    )
                query_vector = query_vectors[0]

            with logfire.span("Semantic search with filters"):
                # JOIN reviews with their embeddings, apply all filter
                # clauses, then ORDER BY cosine distance (ascending =
                # most similar first). The HNSW index on the embedding
                # column makes this an approximate nearest-neighbor search.
                statement = (
                    select(Review)
                    .join(ReviewEmbedding, ReviewEmbedding.review_id == Review.id)
                    .where(*clauses)
                    .order_by(ReviewEmbedding.embedding.cosine_distance(query_vector))
                    .limit(get_settings().SEMANTIC_SEARCH_LIMIT)
                )
                result = await session.execute(statement)
        else:
            # ── Standard path (no keywords) ──────────────────────────
            # Return all matching reviews sorted by date, URL, and product.
            # No limit — the full result set is exported.
            sort_order = [Review.datePublished, Review.url, Review.product]
            statement = select(Review).where(*clauses).order_by(*sort_order)
            result = await session.execute(statement)

    # ── Export + summarize ────────────────────────────────────────────
    with logfire.span("Pick a handler, handle reviews, return a response"):
        # Extract ORM objects from the result set.
        if not (scalars := result.scalars().all()):
            return Response(
                **r.model_dump(),
                comment="Your search did not match any reviews",
            )

        # Instantiate the format-specific handler (CSVMaker, JSONMaker, etc.)
        # based on the outputFormat query parameter. The handler converts
        # ORM objects to a DataFrame, serializes to the target format,
        # uploads to S3, and generates a pre-signed download URL.
        handler_class = available_output_formats[r.outputFormat]
        handler = handler_class(scalars, client)
        await handler.upload_contents()

        url = await handler.generate_url()
        # Use the explicitly requested model, or fall back to the default.
        model_name = r.cloudModel or get_settings().DEFAULT_CLOUD_MODEL
        # Run the LLM map-reduce summarization pipeline on the review texts.
        comment = await handler.summarize_reviews(model_name)

        return Response(
            **r.model_dump(),
            filename=handler.key,
            url=url,
            comment=f"**Summary model:** `{model_name}`\n\n{comment}",
        )


# ── DELETE /reviews ──────────────────────────────────────────────────────────
# Delete specific reviews by their database IDs.
# Protected by api_token. Returns 204 No Content on success.
@router.delete(
    "/reviews",
    dependencies=[Depends(api_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_reviews(
    delete_ids: list[int],
    session: DBSession,
):
    """Delete reviews by a list of database IDs.

    Embeddings are automatically deleted via ON DELETE CASCADE on the
    ReviewEmbedding foreign key.
    """
    with logfire.span("Delete entries and commit"):
        statement = delete(Review).where(Review.id.in_(delete_ids))
        await session.execute(statement)
        await session.commit()


# ── DELETE /reviews/by-date ──────────────────────────────────────────────────
# Delete all reviews within a date range. Useful for re-crawling a specific
# period (delete old data, then re-crawl to get fresh data).
@router.delete(
    "/reviews/by-date",
    dependencies=[Depends(api_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_reviews_by_date(
    start_date: Annotated[date, Query(alias="startDate")],
    end_date: Annotated[date, Query(alias="endDate")],
    session: DBSession,
):
    """Delete all reviews published within [startDate, endDate]."""
    with logfire.span("Delete entries by date range and commit"):
        # Use datetime boundaries instead of cast(… Date) so the B-tree
        # index on datePublished is usable (no function wrapping the column).
        statement = delete(Review).where(
            Review.datePublished >= datetime.combine(start_date, time.min),
            Review.datePublished <= datetime.combine(end_date, time.max),
        )
        result = await session.execute(statement)
        deleted = result.rowcount
        await session.commit()

    logfire.info(
        "deleted {deleted} reviews in [{start}, {end}]",
        deleted=deleted, start=str(start_date), end=str(end_date),
    )


# ── DELETE /reviews/duplicates ───────────────────────────────────────────────
# Deduplicate the reviews table by keeping only the first (lowest ID) review
# for each unique (md5(reviewBody), product) combination.
@router.delete(
    "/reviews/duplicates",
    dependencies=[Depends(api_token)],
)
async def delete_duplicate_reviews(
    session: DBSession,
):
    """Remove duplicate reviews, keeping the earliest (lowest ID) of each group.

    Deduplication key: md5(reviewBody) + product. Using md5() instead of the
    raw text keeps the hash table small during the GROUP BY operation.
    """
    with logfire.span("Delete duplicate entries"):
        # The CTE + md5 dedup does a full sequential scan with
        # HashAggregate — fast for 300 K rows but heavier than a
        # filtered query.  5 min is generous; expect < 2 min.
        await session.execute(text("SET LOCAL statement_timeout = '300s'"))

        # Materialise the keeper ids in a CTE first, then delete via
        # integer-only NOT IN.  The previous single-statement form
        #   DELETE … WHERE id NOT IN (SELECT min(id) … GROUP BY reviewBody, product)
        # forced Postgres to hash/sort every reviewBody text value and
        # re-evaluate the subquery, which could hang on large tables.
        #
        # GROUP BY md5(reviewBody) instead of the raw text keeps the
        # hash table small (32-byte strings vs full review bodies).
        # Postgres uses HashAggregate for the full-table scan — no index
        # needed.  MD5 collisions on natural-language texts are negligible.
        keep_ids = (
            select(func.min(Review.id).label("id"))
            .group_by(func.md5(Review.reviewBody), Review.product)
            .cte("keep")
        )
        statement = (
            delete(Review)
            .where(Review.id.not_in(select(keep_ids.c.id)))
        )
        result = await session.execute(statement)
        deleted = result.rowcount
        await session.commit()

    logfire.info("deduplicated: {deleted} rows removed", deleted=deleted)

    return {"deleted": deleted}
