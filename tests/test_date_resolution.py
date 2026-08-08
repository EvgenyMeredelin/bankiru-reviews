"""The unified rule: an omitted bound falls back to the bound of the data."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from bankiru.api.deps import GATEWAY_HEADER, GATEWAY_HEADER_VALUE

MIN = datetime(2025, 1, 1, 8, 30)
MAX = datetime(2026, 8, 7, 21, 45)
BOUNDS = (MIN, MAX)


@pytest.mark.parametrize("summarize", [False, True])
async def test_omitted_start_resolves_to_min(api, summarize):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"endDate": "2025-02-01", "summarize": summarize},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2025-02-01"


@pytest.mark.parametrize("summarize", [False, True])
async def test_omitted_end_resolves_to_max(api, summarize):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2026-06-01", "summarize": summarize},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["startDate"] == "2026-06-01"
    assert body["endDate"] == "2026-08-07"


async def test_both_omitted_span_the_stored_data(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2026-08-07"


async def test_a_guest_through_the_gateway_gets_the_same_bounds(api):
    """The rule is caller-independent — the public Nginx path included."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            headers={
                GATEWAY_HEADER: GATEWAY_HEADER_VALUE,
                "API-Token": "test-guest-token",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2026-08-07"


async def test_plain_date_bounds_are_accepted(api):
    """min()/max() may come back as ``date`` rather than ``datetime``."""
    client, _, _ = api(bounds=(date(2025, 1, 1), date(2026, 8, 7)), rows=[])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2026-08-07"


async def test_no_bounds_query_when_both_dates_given(api):
    """Callers that pass both dates must not pay for the extra round-trip."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get(
            "/reviews",
            params={"startDate": "2026-01-01", "endDate": "2026-01-31"},
        )

    assert session.bounds_queries == 0
    assert session.main_queries == 1


async def test_bounds_do_not_depend_on_the_current_date(api):
    """The upper bound is max(datePublished), never "today".

    With the newest review two years old, an omitted endDate must resolve to
    that date — the old behaviour stretched the interval to the current day.
    """
    stale = (datetime(2024, 1, 10), datetime(2024, 3, 20))
    client, _, _ = api(bounds=stale, rows=[])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert body["startDate"] == "2024-01-10"
    assert body["endDate"] == "2024-03-20"


async def test_bounds_query_ignores_the_other_filters(api):
    """Bounds are global, otherwise a narrow filter could dodge the limit."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"bankName": "Тестбанк"})

    assert session.bounds_queries == 1
    assert "min(" in session.bounds_sql.lower()
    assert "max(" in session.bounds_sql.lower()
    assert "where" not in session.bounds_sql.lower()
