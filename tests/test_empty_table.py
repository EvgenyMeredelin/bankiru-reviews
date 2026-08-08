"""An empty table has no bounds to resolve — answer, do not fail."""

from __future__ import annotations

from bankiru.api.routes import NO_RESULTS_COMMENT


async def test_empty_table_returns_no_results(api):
    client, _, _ = api(bounds=(None, None), rows=[])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert response.status_code == 200
    assert body["comment"] == NO_RESULTS_COMMENT
    assert body["startDate"] is None
    assert body["endDate"] is None


async def test_empty_table_skips_the_main_query(api):
    """Nothing to select, so the handler returns before touching the table."""
    client, session, _ = api(bounds=(None, None), rows=[])
    async with client:
        await client.get("/reviews")

    assert session.bounds_queries == 1
    assert session.main_queries == 0


async def test_empty_table_does_not_trip_the_summarize_limit(api):
    """No data means no oversized interval — 200, as before the change."""
    client, _, _ = api(bounds=(None, None), rows=[])
    async with client:
        response = await client.get("/reviews", params={"summarize": True})

    assert response.status_code == 200
    assert response.json()["comment"] == NO_RESULTS_COMMENT


async def test_explicit_dates_take_the_ordinary_no_results_path(api):
    """A different branch: with both dates given nothing is resolved at all.

    The bounds query never runs, so an empty table is indistinguishable from
    filters that matched nothing — and the echo carries the request values
    rather than the nulls of the empty-table exception.
    """
    client, session, _ = api(bounds=(None, None), rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2026-01-01", "endDate": "2026-01-31"},
        )

    body = response.json()
    assert session.bounds_queries == 0
    assert session.main_queries == 1
    assert body["comment"] == NO_RESULTS_COMMENT
    assert body["startDate"] == "2026-01-01"
    assert body["endDate"] == "2026-01-31"


async def test_one_given_bound_still_resolves_against_nothing(api):
    """A single bound is not enough to skip resolution — the nulls come back."""
    client, session, _ = api(bounds=(None, None), rows=[])
    async with client:
        response = await client.get("/reviews", params={"startDate": "2026-01-01"})

    body = response.json()
    assert session.bounds_queries == 1
    assert session.main_queries == 0
    assert body["comment"] == NO_RESULTS_COMMENT
    assert body["startDate"] == "2026-01-01"
    assert body["endDate"] is None
