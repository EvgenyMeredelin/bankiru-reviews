"""An inverted effective range is an error, not an empty result."""

from __future__ import annotations

from datetime import datetime

import pytest

from bankiru.api.routes import INVERTED_RANGE_DETAIL

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


@pytest.mark.parametrize("summarize", [False, True])
async def test_explicitly_inverted_dates(api, summarize):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "startDate": "2026-05-01",
                "endDate": "2026-04-01",
                "summarize": summarize,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == INVERTED_RANGE_DETAIL


@pytest.mark.parametrize("summarize", [False, True])
async def test_start_after_the_last_stored_review(api, summarize):
    """startDate in the future with an omitted endDate inverts the range."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2030-01-01", "summarize": summarize},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == INVERTED_RANGE_DETAIL


async def test_end_before_the_first_stored_review(api):
    """endDate below the earliest review inverts it from the other side."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"endDate": "2020-01-01"})

    assert response.status_code == 400


async def test_inverted_range_skips_the_main_query(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"startDate": "2030-01-01"})

    assert session.main_queries == 0


async def test_inversion_is_reported_before_the_summarize_limit(api):
    """Both rules are broken; the inverted range is checked first.

    An inverted range is also, formally, longer than three months once the
    bounds are swapped — telling the caller about the span would send them
    narrowing a range that is simply backwards.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "startDate": "2026-08-01",
                "endDate": "2025-01-01",
                "summarize": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == INVERTED_RANGE_DETAIL
