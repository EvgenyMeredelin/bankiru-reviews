"""HTTP routes for /reviews and root redirect.

This module defines all HTTP endpoints for the API service:

  Unauthenticated:
    GET  /healthz              — Docker healthcheck (excluded from Logfire tracing)
    GET  /                     — redirect to /docs (Swagger UI)

  GET /reviews:
    Internal (no X-Bankiru-Gateway) — no token (Gradio UI after Authentik)
    Via public Nginx gateway       — API-Token ∈ GUEST_API_TOKEN or API_TOKEN

  Privileged (API-Token must match API_TOKEN; guests rejected):
    POST   /reviews            — bulk insert reviews (called by the parser)
    DELETE /reviews            — delete reviews by ID list
    DELETE /reviews/by-date    — delete reviews within a date range
    DELETE /reviews/duplicates — deduplicate reviews by md5(reviewBody) + product

The GET /reviews endpoint is the most complex: it builds a dynamic SQLAlchemy
query from the filter parameters, optionally performs semantic search via
pgvector, then either returns reviews inline (no outputFormat) or exports to
S3. LLM summarization runs only when the effective ``summarize`` flag is true
(default false on the public gateway, true for internal UI calls).

Connection to other modules:
  - bankiru.api.schemas    — Pydantic models for request/response validation
  - bankiru.api.deps       — FastAPI dependencies (DBSession, BotoClient, api_token)
  - bankiru.api.handlers   — format-specific export handlers (CSV, JSON, Parquet, XLSX)
  - bankiru.api.summarizer — LLM map-reduce summarization
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
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, insert, or_, select, text, tuple_
from starlette.requests import Request as HttpRequest
from starlette.responses import RedirectResponse

from bankiru.api import schemas
from bankiru.api.deps import (
    GATEWAY_HEADER,
    GATEWAY_HEADER_VALUE,
    BotoClient,
    DBSession,
    api_token,
    guest_or_admin_token_if_gateway,
)
from bankiru.api.schemas import (
    Response,
    ReviewOut,
    ReviewsQuery,
    available_output_formats,
)
from bankiru.api.summarizer import summarize_map_reduce
from bankiru.config import get_settings
from bankiru.embedder import embed_texts, format_review_for_embedding
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
    """Merge newly inserted rows into date-stamped Parquet backups on S3.

    Key shape::
      {OBS_BACKUP_PREFIX}/bankiru-reviews-{YYYY-MM-DD}.parquet

    Rows are grouped by ``datePublished`` calendar date (review date, not
    crawl/POST day). Each group merges into its own object: download if
    present, concatenate, drop duplicate ``(url, product)`` pairs keeping
    the latest row. Multi-day backfills therefore land in the correct
    per-date files; a second POST that inserts more rows for an existing
    review date accumulates instead of overwriting.

    *rows* is the in-batch-deduped payload (newly inserted and/or already
    stored pairs). Callers use it both after insert and on all-skipped
    retries so a prior OBS miss can heal. Parquet work is offloaded to a
    thread for ``/healthz``.

    Args:
        rows: Review dicts to merge (already validated by Pydantic).
        client: Async S3 client for download/upload.
    """
    settings = get_settings()

    def _group_by_review_date(rows_local: list[dict]) -> dict[str, list[dict]]:
        """Split rows into {ISO date -> row dicts} by datePublished."""
        df = pd.DataFrame.from_records(rows_local)
        dates = pd.to_datetime(df["datePublished"]).dt.date
        grouped: dict[str, list[dict]] = {}
        for date_val, group in df.groupby(dates, sort=False):
            key = (
                date_val.isoformat()
                if hasattr(date_val, "isoformat")
                else date.today().isoformat()
            )
            grouped[key] = group.to_dict(orient="records")
        return grouped

    by_date = await asyncio.to_thread(_group_by_review_date, rows)

    for date_str, date_rows in by_date.items():
        key = f"{settings.OBS_BACKUP_PREFIX}/bankiru-reviews-{date_str}.parquet"

        existing_bytes: bytes | None = None
        try:
            resp = await client.get_object(Bucket=settings.OBS_BUCKET, Key=key)
            # Enter Body only for connection cleanup; read via StreamingBody
            # (``async with Body as stream`` yields aiohttp ClientResponse).
            body = resp["Body"]
            async with body:
                existing_bytes = await body.read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
            # First backup for this date — nothing to merge.

        def _serialize(
            existing: bytes | None,
            new_rows: list[dict] = date_rows,
        ) -> io.BytesIO:
            """CPU-bound: merge + Parquet encode. Runs in a thread."""
            new_df = pd.DataFrame.from_records(new_rows)
            if existing is not None:
                old_df = pd.read_parquet(io.BytesIO(existing))
                merged = pd.concat([old_df, new_df], ignore_index=True)
            else:
                merged = new_df
            # Keep the last occurrence so a re-insert of the same pair (after a
            # manual delete) refreshes the backup row.
            if {"url", "product"}.issubset(merged.columns):
                merged = merged.drop_duplicates(
                    subset=["url", "product"], keep="last"
                )
            buf = io.BytesIO()
            merged.to_parquet(buf, index=False)
            buf.seek(0)
            return buf

        buf = await asyncio.to_thread(_serialize, existing_bytes)
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
      2. Dedupe the batch and skip pairs already stored under (url, product)
         — one review page may yield multiple rows (one per product tag);
         there is intentionally no UNIQUE on url
      3. Bulk INSERT remaining rows in a single SQL statement
      4. Generate vector embeddings for the new reviews (for semantic search)
      5. Merge rows into per-datePublished Parquet backups on S3 (best-effort)

    Returns ``{"inserted": N, "skipped": M}`` (including empty ``[]`` →
    ``{"inserted": 0, "skipped": 0}``). If embedding generation fails, the
    reviews are still inserted — missing embeddings will be backfilled on
    the next API restart (see app.py lifespan).

    Already-stored ``(url, product)`` pairs are skipped, not updated: a
    re-crawl does not refresh ``reviewBody`` / ``location`` / ``bankName``.
    Idempotency is application-level (safe for the serial parser); concurrent
    POSTs of the same pair can still race without a DB unique constraint.

    S3 backup is best-effort relative to Postgres (reviews stay committed
    if PutObject fails) but the request returns **503** on backup failure
    so the parser retries. An all-skipped POST still merges the payload
    into OBS; failure there also returns 503. DELETE endpoints do not
    prune OBS.
    """
    # Fast path: an empty JSON array is valid but there is nothing to insert,
    # embed, or back up. Same response shape as an all-skipped batch.
    if not reviews:
        return {"inserted": 0, "skipped": 0}

    with logfire.span("Create new entries"):
        # Validate via Pydantic, then pass raw dicts straight to a bulk
        # INSERT instead of instantiating N ORM objects + N individual
        # INSERT statements.  This emits a single
        #   INSERT INTO … VALUES (…), (…), …
        # round-trip regardless of batch size.
        rows = [r.model_dump() for r in reviews]

    with logfire.span("Dedupe batch and skip already-stored (url, product)"):
        # One review page can have multiple rows — one per applied product
        # tag.  Idempotency key is therefore (url, product), not url alone.
        # In-batch dedupe first, then skip pairs already in the DB (retried
        # POSTs after a committed insert must not multiply rows).
        # Skip is insert-only: existing pairs are not upserted.
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for row in rows:
            key = (row["url"], row["product"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        in_batch_dupes = len(rows) - len(deduped)
        # Full in-batch-deduped payload for S3 (insert path + all-skipped heal).
        payload_rows = deduped
        rows = deduped

        skipped_existing = 0
        if rows:
            pairs = [(row["url"], row["product"]) for row in rows]
            existing = (
                await session.execute(
                    select(Review.url, Review.product).where(
                        tuple_(Review.url, Review.product).in_(pairs)
                    )
                )
            ).all()
            existing_pairs = {(u, p) for u, p in existing}
            if existing_pairs:
                before = len(rows)
                rows = [
                    row
                    for row in rows
                    if (row["url"], row["product"]) not in existing_pairs
                ]
                skipped_existing = before - len(rows)

        skipped_total = in_batch_dupes + skipped_existing
        if skipped_total:
            logfire.info(
                "skipped {n} rows "
                "(in_batch_dupes={ib}, already_stored={ex}; {kept} new)",
                n=skipped_total,
                ib=in_batch_dupes,
                ex=skipped_existing,
                kept=len(rows),
            )
        if not rows:
            with logfire.span("Backup already-stored batch to S3"):
                # Heal OBS after commit-then-backup-fail: parser retries see
                # every pair as existing; merge payload and surface 503 if
                # OBS is still down so retries continue.
                try:
                    await _backup_daily_batch(payload_rows, client)
                except Exception as exc:
                    logfire.warning(
                        "S3 daily backup failed on all-skipped POST: {exc}",
                        exc=str(exc),
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="S3 backup failed; reviews already in DB",
                    ) from exc
            return {"inserted": 0, "skipped": skipped_total}

    with logfire.span("Bulk insert and commit"):
        await session.execute(insert(Review), rows)
        await session.commit()
        inserted = len(rows)

    with logfire.span("Embed new reviews"):
        try:
            # Match the just-inserted (url, product) pairs.  Multiple rows
            # may share a URL (different product tags); do not key on URL alone.
            # LEFT JOIN + IS NULL excludes reviews that already have embeddings.
            pairs = [(row["url"], row["product"]) for row in rows]
            id_stmt = (
                select(
                    Review.id,
                    Review.bankName,
                    Review.product,
                    Review.location,
                    Review.reviewBody,
                )
                .outerjoin(ReviewEmbedding, ReviewEmbedding.review_id == Review.id)
                .where(
                    tuple_(Review.url, Review.product).in_(pairs),
                    ReviewEmbedding.review_id.is_(None),
                )
            )
            id_result = await session.execute(id_stmt)
            id_rows = id_result.all()

            if id_rows:
                review_ids = [row.id for row in id_rows]
                texts = [
                    format_review_for_embedding(
                        bank_name=row.bankName,
                        product=row.product,
                        location=row.location,
                        review_body=row.reviewBody,
                    )
                    for row in id_rows
                ]
                vectors = await embed_texts(texts, mode="passage")

                embedding_rows = [
                    {"review_id": rid, "embedding": vec}
                    for rid, vec in zip(review_ids, vectors)
                ]
                await session.execute(insert(ReviewEmbedding), embedding_rows)
                await session.commit()
        except Exception as exc:
            # Rollback the failed embedding INSERT so the session is clean
            # for any subsequent operations and for proper cleanup by the
            # FastAPI dependency injection teardown.
            await session.rollback()
            logfire.warning(
                "embedding failed for POST batch, will be backfilled: {exc}",
                exc=str(exc),
            )

    with logfire.span("Backup daily batch to S3"):
        # Merge the full payload (new + already-stored in this request) so
        # overlapping backfills heal OBS for skipped pairs too. Reviews are
        # already committed: on PutObject failure return 503 so the parser
        # retries (idempotent insert + all-skipped heal), not 201.
        try:
            await _backup_daily_batch(payload_rows, client)
        except Exception as exc:
            logfire.warning(
                "S3 daily backup failed after commit (reviews kept): {exc}",
                exc=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="S3 backup failed; reviews already in DB",
            ) from exc

    return {"inserted": inserted, "skipped": skipped_total}


def _effective_summarize(r: ReviewsQuery, http_request: HttpRequest) -> bool:
    """Resolve summarize: explicit query wins; else gateway→False, internal→True."""
    if r.summarize is not None:
        return r.summarize
    is_gateway = (
        http_request.headers.get(GATEWAY_HEADER) == GATEWAY_HEADER_VALUE
    )
    return not is_gateway


def _response_base(r: ReviewsQuery, effective_summarize: bool) -> dict:
    """Echo query fields with summarize set to the effective boolean."""
    return {**r.model_dump(), "summarize": effective_summarize}


# ── GET /reviews ─────────────────────────────────────────────────────────────
# Main query endpoint. Internal UI calls need no token; requests proxied
# through the public Nginx gateway must present a guest or admin API-Token.
@router.get(
    "/reviews",
    response_model=Response,
    dependencies=[Depends(guest_or_admin_token_if_gateway)],
)
async def get_reviews(
    http_request: HttpRequest,
    r: Annotated[ReviewsQuery, Query()],
    session: DBSession,
    client: BotoClient,
):
    """Query reviews with filters; optionally export to S3 and/or summarize.

    Auth: none for internal callers; via public gateway require API-Token
    matching GUEST_API_TOKEN or API_TOKEN (see guest_or_admin_token_if_gateway).

    Query parameters (all optional):
      - startDate/endDate: date range filter (B-tree index on datePublished)
      - bankName: list of bank names (exact match via IN clause)
      - product: list of product labels (exact match via IN clause)
      - location: list of city prefixes (startswith match)
      - keywords: free-text semantic search (pgvector cosine similarity)
      - outputFormat: if omitted, return reviews inline; if set, S3 export
      - summarize: if omitted, false on gateway / true internally
      - cloudModel: LLM model when summarize is effective true

    Returns a Response with either ``reviews`` (inline) or ``url``/``filename``
    (export), plus optional ``comment`` (LLM summary or no-results message).
    """
    effective_summarize = _effective_summarize(r, http_request)

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

        # ── Semantic search path (keywords query param) ──────────────
        # When a semantic search query is provided, embed it and find the
        # most similar reviews using pgvector cosine distance.
        # The result is ordered by similarity (closest first) and capped
        # at SEMANTIC_SEARCH_LIMIT reviews.
        if r.keywords and r.keywords.strip():
            search_settings = get_settings()
            with logfire.span("Embed semantic search query"):
                try:
                    # Embed the user's query text into a vector using the
                    # same model that was used to embed the review texts.
                    query_vectors = await embed_texts(
                        [r.keywords.strip()],
                        mode="query",
                    )
                except Exception as exc:
                    logfire.warning(
                        "Failed to embed semantic search query: {exc}", exc=str(exc),
                    )
                    # Fail gracefully: return an error message instead of
                    # crashing. The user can retry without a semantic search query.
                    return Response(
                        **_response_base(r, effective_summarize),
                        comment=f"Semantic search unavailable: {exc}",
                    )
                query_vector = query_vectors[0]

            with logfire.span("Semantic search with filters"):
                # Higher ef_search improves HNSW recall during approximate search.
                # Use a validated literal — PostgreSQL SET does not accept bind params.
                # Explicit int() cast provides defense-in-depth against injection.
                ef_search = int(max(1, search_settings.SEMANTIC_SEARCH_EF_SEARCH))
                await session.execute(
                    text(f"SET LOCAL hnsw.ef_search = {ef_search}"),
                )
                distance = ReviewEmbedding.embedding.cosine_distance(query_vector)
                search_clauses = list(clauses)
                if search_settings.SEMANTIC_SEARCH_MAX_DISTANCE is not None:
                    search_clauses.append(
                        distance <= search_settings.SEMANTIC_SEARCH_MAX_DISTANCE
                    )
                # JOIN reviews with their embeddings, apply all filter
                # clauses, then ORDER BY cosine distance (ascending =
                # most similar first). The HNSW index on the embedding
                # column makes this an approximate nearest-neighbor search.
                statement = (
                    select(Review)
                    .join(ReviewEmbedding, ReviewEmbedding.review_id == Review.id)
                    .where(*search_clauses)
                    .order_by(distance)
                    .limit(search_settings.SEMANTIC_SEARCH_LIMIT)
                )
                result = await session.execute(statement)
        else:
            # ── Standard path (no semantic search query) ─────────────
            # Return all matching reviews sorted by date, URL, and product.
            # No limit — the full result set is exported or returned inline.
            sort_order = [Review.datePublished, Review.url, Review.product]
            statement = select(Review).where(*clauses).order_by(*sort_order)
            result = await session.execute(statement)

    # ── Inline or export; optional summarize ──────────────────────────
    with logfire.span("Handle reviews and return a response"):
        if not (scalars := result.scalars().all()):
            return Response(
                **_response_base(r, effective_summarize),
                comment="Your search did not match any reviews",
            )

        comment: str | None = None
        if effective_summarize:
            with logfire.span("Summarize reviews"):
                model_name = r.cloudModel or get_settings().DEFAULT_CLOUD_MODEL
                # Deduplicate bodies (same as ScalarsHandler.summarize_reviews).
                texts = list(dict.fromkeys(row.reviewBody for row in scalars))
                summary = await summarize_map_reduce(texts, model_name=model_name)
                comment = f"**Summary model:** `{model_name}`\n\n{summary}"

        if r.outputFormat is None:
            reviews = [ReviewOut.model_validate(row) for row in scalars]
            return Response(
                **_response_base(r, effective_summarize),
                reviews=reviews,
                comment=comment,
            )

        handler_class = available_output_formats[r.outputFormat]
        handler = handler_class(scalars, client)
        await handler.upload_contents()
        url = await handler.generate_url()
        return Response(
            **_response_base(r, effective_summarize),
            filename=handler.key,
            url=url,
            comment=comment,
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
