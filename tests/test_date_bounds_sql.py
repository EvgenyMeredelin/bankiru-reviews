"""The resolved bounds reach the SQL filter, inclusive on both ends."""

from __future__ import annotations

from datetime import datetime

from conftest import FakeReview

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


async def test_bounds_are_inclusive(api):
    """Lower bound at 00:00:00, upper at 23:59:59.999999 — a whole day."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get(
            "/reviews",
            params={"startDate": "2026-03-07", "endDate": "2026-03-08"},
        )

    sql = session.main_sql
    assert "'2026-03-07 00:00:00'" in sql
    assert "'2026-03-08 23:59:59.999999'" in sql


async def test_single_day_range_covers_that_day(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get(
            "/reviews",
            params={"startDate": "2026-03-07", "endDate": "2026-03-07"},
        )

    sql = session.main_sql
    assert "'2026-03-07 00:00:00'" in sql
    assert "'2026-03-07 23:59:59.999999'" in sql


async def test_resolved_bounds_are_applied_when_dates_are_omitted(api):
    """The query is always closed on both sides, even for an empty request."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews")

    sql = session.main_sql
    assert "'2025-01-01 00:00:00'" in sql
    assert "'2026-08-07 23:59:59.999999'" in sql


async def test_semantic_search_filters_on_the_resolved_bounds(api, embedder):
    """The vector path reuses the same clauses — not an unbounded search."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь в отделении"})

    sql = session.main_sql
    assert "'2025-01-01 00:00:00'" in sql
    assert "'2026-08-07 23:59:59.999999'" in sql


async def test_summarized_query_filters_on_the_same_bounds(api, summarizer):
    """The summarized corpus must match the interval the limit checked."""
    # A row is required: with an empty result set the handler returns before
    # the summarizer, and the test would prove nothing.
    bounds = (datetime(2026, 6, 1), datetime(2026, 8, 7))
    client, session, _ = api(bounds=bounds, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"summarize": True})

    sql = session.main_sql
    assert "'2026-06-01 00:00:00'" in sql
    assert "'2026-08-07 23:59:59.999999'" in sql
    assert summarizer.calls[0][0] == [FakeReview().reviewBody]
    assert response.json()["comment"].endswith(summarizer.SUMMARY)
