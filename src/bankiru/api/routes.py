"""HTTP routes for /reviews and root redirect."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated

import logfire
from fastapi import APIRouter, Depends, Query, Response as HTTPResponse, status
from sqlalchemy import delete, func, insert, or_, select, text
from starlette.responses import RedirectResponse

from bankiru.api import schemas
from bankiru.api.deps import BotoClient, DBSession, api_token
from bankiru.api.schemas import Request, Response, available_output_formats
from bankiru.config import get_settings
from bankiru.models import Review

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
@logfire.no_auto_trace
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


backup_request = Request(isBackup=True)


@router.get("/", include_in_schema=False)
async def redirect_from_root_to_docs():
    return RedirectResponse(url="/docs")


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

    with logfire.span("Make a database backup"):
        await get_reviews(backup_request, session, client)


@router.get("/reviews")
async def get_reviews(
    r: Annotated[Request, Query()],
    session: DBSession,
    client: BotoClient,
):
    with logfire.span("Select entries"):
        clauses = []

        # Compare datePublished (a DateTime column) directly against
        # datetime boundaries so the B-tree index is usable.  The
        # previous `cast(datePublished, Date)` wrapped the column in a
        # function call, forcing a sequential scan.
        if r.startDate:
            clauses.append(Review.datePublished >= datetime.combine(r.startDate, time.min))
        if r.endDate:
            clauses.append(Review.datePublished <= datetime.combine(r.endDate, time.max))
        if r.location:
            clauses.append(or_(*[Review.location.startswith(loc) for loc in r.location]))
        if r.bankName:
            clauses.append(Review.bankName.in_(r.bankName))
        if r.product:
            clauses.append(Review.product.in_(r.product))

        sort_order = [Review.datePublished, Review.url, Review.product]
        statement = select(Review).where(*clauses).order_by(*sort_order)
        result = await session.execute(statement)

    with logfire.span("Pick a handler, handle reviews, return a response"):
        if not (scalars := result.scalars().all()):
            return Response(
                **r.model_dump(),
                comment="Your search did not match any reviews",
            )

        handler_class = available_output_formats[r.outputFormat]
        handler = handler_class(scalars, client, r.isBackup)
        await handler.upload_contents()

        if r.isBackup:
            return HTTPResponse(status_code=status.HTTP_204_NO_CONTENT)

        url = await handler.generate_url()
        model_name = r.cloudModel or get_settings().DEFAULT_CLOUD_MODEL
        comment = await handler.summarize_reviews(model_name)

        return Response(
            **r.model_dump(),
            filename=handler.key,
            url=url,
            comment=f"**Cloud model:** `{model_name}`\n\n{comment}",
        )


@router.delete(
    "/reviews",
    dependencies=[Depends(api_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_reviews(
    delete_ids: list[int],
    session: DBSession,
    client: BotoClient,
):
    with logfire.span("Delete entries and commit"):
        statement = delete(Review).where(Review.id.in_(delete_ids))
        await session.execute(statement)
        await session.commit()

    with logfire.span("Make a database backup"):
        await get_reviews(backup_request, session, client)


@router.delete(
    "/reviews/by-date",
    dependencies=[Depends(api_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_reviews_by_date(
    start_date: Annotated[date, Query(alias="startDate")],
    end_date: Annotated[date, Query(alias="endDate")],
    session: DBSession,
    client: BotoClient,
):
    with logfire.span("Delete entries by date range and commit"):
        # Use datetime boundaries instead of cast(… Date) so the index
        # on datePublished is usable.
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

    if deleted:
        with logfire.span("Make a database backup"):
            await get_reviews(backup_request, session, client)


@router.delete(
    "/reviews/duplicates",
    dependencies=[Depends(api_token)],
)
async def delete_duplicate_reviews(
    session: DBSession,
    client: BotoClient,
):
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

    if deleted:
        with logfire.span("Make a database backup"):
            await get_reviews(backup_request, session, client)

    return {"deleted": deleted}
